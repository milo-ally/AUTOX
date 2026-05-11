"""ChannelEngine — drives the microclaw runtime for one inbound message.

This is the glue between the channels layer and `ConversationRuntime`. It
builds the same stack `MicroclawCli` builds (session, provider, skills,
tools, executor) but with no terminal-specific UI, and forwards runtime
streaming events to a `TurnSink`.

The engine is owned by `microclaw serve`; channels never see it directly.
They only produce `InboundMessage`s and `TurnSink`s.
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

from microclaw import DEFAULT_MODEL
from microclaw.channels.base import InboundMessage, TurnSink
from microclaw.permissions import (
    PermissionMode,
    PermissionOutcome,
    PermissionPolicy,
)
from microclaw.providers import create_provider, resolve_model_alias
from microclaw.providers.types import Usage
from microclaw.runtime import ConversationRuntime, RuntimeConfig
from microclaw.session import Session, new_session
from microclaw.skills import SkillLoader
from microclaw.tools.executor import ToolExecutor
from microclaw.tools.registry import ToolRegistry


# -- Permission prompter for non-TTY channels -------------------------------


class ChannelDenyPrompter:
    """Default prompter for channels: deny any tool that needs escalation.

    Channels run unattended, so we never want to silently grant elevated
    permissions. Operators must launch `microclaw serve --permission-mode ...`
    explicitly when they want write/full access for a given channel.

    A future ChannelPrompter could forward the request through the channel
    itself (e.g. "May I run this command? reply yes/no") — that lives outside
    this MVP.
    """

    def __init__(self, mode: PermissionMode):
        self.mode = mode
        self.escalated = False

    def ask(
        self,
        tool_name: str,
        tool_input: str,
        required_permission: PermissionMode,
        current_mode: PermissionMode,
    ) -> PermissionOutcome:
        return PermissionOutcome.deny(
            f"tool `{tool_name}` needs {required_permission} but channel runs in "
            f"{current_mode}; restart `microclaw serve` with a higher "
            f"--permission-mode to allow it"
        )


# -- Engine -----------------------------------------------------------------


class ChannelEngine:
    """A headless microclaw runtime host driven by channels.

    One engine instance carries one logical conversation (Session) and is
    reused across inbound messages from the same channel/session pair. The
    engine is not thread-safe; serving multiple sessions concurrently means
    one engine per session.
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        permission_mode: PermissionMode = PermissionMode.READ_ONLY,
        allowed_tools: set[str] | None = None,
        session: Session | None = None,
        workspace_dir: str | None = None,
    ):
        self.model = resolve_model_alias(model)
        self.permission_mode = permission_mode
        self.allowed_tools = allowed_tools
        self.workspace_dir = workspace_dir or os.getcwd()

        self.session = session or new_session()
        self.session.model = self.model

        self.provider = create_provider(self.model)

        self.skill_loader = SkillLoader()
        self.active_skills: list[str] = []

        self.tool_registry = ToolRegistry(allowed_tools=allowed_tools)
        self.tool_executor = ToolExecutor(
            allowed_tools=allowed_tools,
            skill_loader=self.skill_loader,
        )

        from microclaw.tools.team import TeamManager

        self.team_manager = TeamManager(model=self.model)
        self.tool_executor.set_team_manager(self.team_manager)

        self.system_prompt = self._build_system_prompt()
        self._cumulative_usage = Usage()

    # -- prompt -------------------------------------------------------------

    def _build_system_prompt(self) -> list[str]:
        tool_names = ", ".join(self.tool_registry.allowed_tool_names())
        prompt = [
            "You are microclaw, an agentic coding assistant. You are reached "
            "through an external channel (mobile, WeChat, web, queue, ...), "
            "so keep replies concise and self-contained.",
            f"Current working directory: {self.workspace_dir}",
            "Prefer minimal, focused changes. Always verify your work.",
            f"You can use the following tools: {tool_names}.",
            "Skills are available for specialized workflows. Read the skill "
            "catalog below and use the load_skill tool before tackling "
            "unfamiliar topics that match one of these skills.",
            f"Skills available:\n{self.skill_loader.get_descriptions_text(self.active_skills)}",
        ]
        for block in self.skill_loader.get_active_skill_prompt_blocks(self.active_skills):
            prompt.append(block)
        return prompt

    # -- runtime ------------------------------------------------------------

    def _build_runtime(self) -> ConversationRuntime:
        config = RuntimeConfig(
            system_prompt=self.system_prompt,
            permission_mode=self.permission_mode,
            allowed_tools=self.allowed_tools,
        )
        return ConversationRuntime(
            session=self.session,
            provider=self.provider,
            tool_executor=self.tool_executor,
            tool_registry=self.tool_registry,
            config=config,
        )

    # -- main entry ---------------------------------------------------------

    def handle_message(
        self,
        inbound: InboundMessage,
        sink: TurnSink,
        *,
        use_streaming: bool = True,
        permission_mode: PermissionMode | None = None,
        prompter: object | None = None,
    ) -> None:
        """Run one agent turn for the given inbound message.

        Events flow:

            sink.on_turn_start
              -> sink.on_text_delta / on_thinking_delta / on_tool_*
              -> sink.on_final(full_text)
              -> sink.on_turn_end

        Errors propagate as sink.on_error and the turn still ends cleanly.
        The engine catches everything — channels should never crash because
        of a single bad turn.
        """
        request_id = f"req_{uuid.uuid4().hex[:12]}"
        effective_permission_mode = permission_mode or self.permission_mode
        prompter = prompter or ChannelDenyPrompter(effective_permission_mode)
        original_permission_mode = self.permission_mode
        self.permission_mode = effective_permission_mode
        runtime = self._build_runtime()
        self.permission_mode = original_permission_mode
        starting_message_count = len(self.session.messages)

        try:
            sink.on_turn_start(request_id, inbound)
        except Exception:
            # A broken sink lifecycle hook should not stop the turn.
            pass

        try:
            if use_streaming:
                def on_event(event_type: str, data: Any) -> None:
                    if event_type == "text":
                        sink.on_text_delta(str(data))
                    elif event_type == "thinking":
                        sink.on_thinking_delta(str(data))
                    elif event_type == "tool_execution_start":
                        sink.on_tool_start(
                            str(data.get("name", "tool")),
                            data.get("input") or {},
                        )
                    elif event_type == "tool_execution_end":
                        sink.on_tool_end(
                            str(data.get("name", "tool")),
                            data.get("input") or {},
                            bool(data.get("is_error")),
                            str(data.get("result") or ""),
                        )
                    # tool_use_start / tool_use_delta / tool_use_complete /
                    # final_message / usage are not surfaced to channels.

                summary = runtime.run_turn_streaming(
                    inbound.text,
                    on_event=on_event,
                    prompter=prompter,
                )
            else:
                summary = runtime.run_turn(inbound.text, prompter=prompter)

        except KeyboardInterrupt:
            # Roll back to the user message and let the caller decide.
            self.session = runtime.session
            self.session.messages = self.session.messages[: starting_message_count + 1]
            self._cumulative_usage.accumulate(runtime._cumulative_usage)
            self.session.save()
            try:
                sink.on_error(KeyboardInterrupt("turn interrupted"))
            finally:
                self._safe_turn_end(sink, request_id)
            raise

        except Exception as e:
            try:
                sink.on_error(e)
            finally:
                self._safe_turn_end(sink, request_id)
            # Persist partial session state if anything got written.
            try:
                self.session = runtime.session
                self._cumulative_usage.accumulate(runtime._cumulative_usage)
                self.session.save()
            except Exception:
                pass
            return

        # Success path — extract the final assistant text once.
        final_text_parts: list[str] = []
        for msg in summary.assistant_messages:
            txt = msg.text_content()
            if txt:
                final_text_parts.append(txt)
        final_text = "\n\n".join(final_text_parts)

        try:
            sink.on_final(final_text)
        finally:
            self._safe_turn_end(sink, request_id, summary=summary)

        # Persist session + usage.
        self.session = runtime.session
        self._cumulative_usage.accumulate(runtime._cumulative_usage)
        self.session.save()

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _safe_turn_end(sink: TurnSink, request_id: str, summary: Any | None = None) -> None:
        try:
            sink.on_turn_end(request_id, summary=summary)
        except Exception:
            pass


__all__ = ["ChannelEngine", "ChannelDenyPrompter"]
