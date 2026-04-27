"""Conversation runtime — the agentic loop that drives the model ↔ tool cycle."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from microclaw.compact import compact_session, estimate_tokens, micro_compact, THRESHOLD
from microclaw.permissions import PermissionMode, PermissionPolicy
from microclaw.providers.types import ContentBlock, Message, TurnSummary, Usage
from microclaw.session import Session
from microclaw.tools.executor import CompactRequested, ToolError, ToolExecutor
from microclaw.tools.registry import ToolRegistry


class TurnError(Exception):
    """Error returned when a conversation turn cannot be completed."""

    pass


@dataclass
class RuntimeConfig:
    """Configuration for the conversation runtime."""

    max_iterations: int = 200
    system_prompt: list[str] = field(default_factory=list)
    permission_mode: PermissionMode = PermissionMode.READ_ONLY
    allowed_tools: set[str] | None = None
    inbox_reader: Callable[[], list[dict[str, Any]]] | None = None


class ConversationRuntime:
    """Coordinates the model loop, tool execution, and session updates.

    This is the core "agentic loop":
    1. Send messages + system prompt to the provider
    2. Collect the assistant's response (text + tool_use blocks)
    3. If tool_use blocks exist, execute each tool and add results
    4. Repeat until the assistant responds without tool calls
    """

    def __init__(
        self,
        session: Session,
        provider: Any,  # AnthropicProvider or OpenAIProvider
        tool_executor: ToolExecutor,
        tool_registry: ToolRegistry,
        config: RuntimeConfig,
    ):
        self.session = session
        self.provider = provider
        self.tool_executor = tool_executor
        self.tool_registry = tool_registry
        self.config = config
        self.permission_policy = PermissionPolicy(config.permission_mode)
        self._cumulative_usage = Usage()

    def run_turn(
        self,
        user_input: str,
        prompter: Any = None,
    ) -> TurnSummary:
        """Run a full conversation turn (user input → model response, with tool loop).

        Args:
            user_input: The user's text input.
            prompter: Optional interactive permission prompter.

        Returns:
            TurnSummary with the results of the turn.

        Raises:
            TurnError: If the turn cannot be completed.
        """
        self.session.push_user_text(user_input)

        assistant_messages: list[Message] = []
        tool_results: list[Message] = []
        iterations = 0

        while True:
            iterations += 1
            if iterations > self.config.max_iterations:
                raise TurnError(
                    "conversation loop exceeded the maximum number of iterations"
                )

            # Layer 0: check lead inbox (team messaging)
            if self.config.inbox_reader:
                inbox = self.config.inbox_reader()
                if inbox:
                    self.session.push_user_text(
                        f"<inbox>{json.dumps(inbox, ensure_ascii=False)}</inbox>"
                    )

            # Layer 1: micro_compact — silently trim old tool results
            micro_compact(self.session.messages)

            # Layer 2: auto_compact — if token estimate exceeds threshold
            if estimate_tokens(self.session.messages) > THRESHOLD:
                compact_session(self.session, provider=self.provider)

            # Build the API request
            system_text = "\n\n".join(self.config.system_prompt) if self.config.system_prompt else None
            tools = self.tool_registry.tool_definitions() or None

            # Send to provider
            try:
                assistant_message, turn_usage = self.provider.send_turn(
                    messages=self.session.messages,
                    tools=tools,
                    system=system_text,
                )
            except Exception as e:
                raise TurnError(f"API request failed: {e}") from e

            # Accumulate usage
            self._cumulative_usage.input_tokens += turn_usage.input_tokens
            self._cumulative_usage.output_tokens += turn_usage.output_tokens
            self._cumulative_usage.cache_creation_input_tokens += turn_usage.cache_creation_input_tokens
            self._cumulative_usage.cache_read_input_tokens += turn_usage.cache_read_input_tokens

            self.session.push_message(assistant_message)
            assistant_messages.append(assistant_message)

            # Check for tool uses
            pending_tool_uses = assistant_message.tool_uses()
            if not pending_tool_uses:
                break

            # Execute each tool
            manual_compact = False
            for tool_block in pending_tool_uses:
                tool_name = tool_block.name or ""
                tool_use_id = tool_block.id or ""
                tool_input = tool_block.input or {}

                # Permission check
                required_perm = self.tool_registry.required_permission(tool_name)
                outcome = self.permission_policy.authorize(
                    tool_name,
                    json.dumps(tool_input),
                    required_perm,
                    prompter,
                )

                if outcome.allowed:
                    # Execute the tool
                    try:
                        result_str = self.tool_executor.execute(tool_name, tool_input)
                        is_error = False
                    except CompactRequested as cr:
                        # Layer 3: manual compact triggered by the compact tool
                        manual_compact = True
                        result_str = json.dumps({"status": "compacting", "focus": cr.focus})
                        is_error = False
                    except ToolError as e:
                        result_str = json.dumps({"error": str(e)})
                        is_error = True
                else:
                    result_str = json.dumps({"error": outcome.reason})
                    is_error = True

                result_message = Message.tool_result(
                    tool_use_id=tool_use_id,
                    tool_name=tool_name,
                    output=result_str,
                    is_error=is_error,
                )
                self.session.push_message(result_message)
                tool_results.append(result_message)

            # Layer 3: if compact tool was called, perform compaction and end the turn
            if manual_compact:
                compact_session(self.session, provider=self.provider)
                break

        summary = TurnSummary(
            assistant_messages=assistant_messages,
            tool_results=tool_results,
            iterations=iterations,
            usage=self._cumulative_usage,
        )
        return summary

    def run_turn_streaming(
        self,
        user_input: str,
        on_event: Callable[[str, Any], None] | None = None,
        prompter: Any = None,
    ) -> TurnSummary:
        """Run a conversation turn with streaming output.

        Args:
            user_input: The user's text input.
            on_event: Callback for streaming events: (event_type, data).
            prompter: Optional interactive permission prompter.

        Returns:
            TurnSummary with the results of the turn.
        """
        self.session.push_user_text(user_input)

        assistant_messages: list[Message] = []
        tool_results: list[Message] = []
        iterations = 0

        while True:
            iterations += 1
            if iterations > self.config.max_iterations:
                raise TurnError(
                    "conversation loop exceeded the maximum number of iterations"
                )

            # Layer 0: check lead inbox (team messaging)
            if self.config.inbox_reader:
                inbox = self.config.inbox_reader()
                if inbox:
                    self.session.push_user_text(
                        f"<inbox>{json.dumps(inbox, ensure_ascii=False)}</inbox>"
                    )

            # Layer 1: micro_compact — silently trim old tool results
            micro_compact(self.session.messages)

            # Layer 2: auto_compact — if token estimate exceeds threshold
            if estimate_tokens(self.session.messages) > THRESHOLD:
                compact_session(self.session, provider=self.provider)

            system_text = "\n\n".join(self.config.system_prompt) if self.config.system_prompt else None
            tools = self.tool_registry.tool_definitions() or None

            # Stream from provider
            try:
                text_parts: list[str] = []
                thinking_parts: list[str] = []
                tool_uses_acc: dict[int, dict[str, Any]] = {}
                final_message: Message | None = None

                for event_type, data in self.provider.stream_turn(
                    messages=self.session.messages,
                    tools=tools,
                    system=system_text,
                ):
                    if on_event:
                        on_event(event_type, data)

                    if event_type == "text":
                        text_parts.append(data)
                    elif event_type == "thinking":
                        thinking_parts.append(data)
                    elif event_type == "tool_use_start":
                        idx = data.get("index", len(tool_uses_acc))
                        tool_uses_acc[idx] = {
                            "id": data.get("id", ""),
                            "name": data.get("name", ""),
                            "arguments": "",
                        }
                    elif event_type == "tool_use_delta":
                        # Append to the most recently started tool call
                        if tool_uses_acc:
                            last_idx = max(tool_uses_acc.keys())
                            tool_uses_acc[last_idx]["arguments"] += data
                    elif event_type == "tool_use_complete":
                        idx = data.get("index", 0)
                        if idx in tool_uses_acc:
                            tool_uses_acc[idx]["id"] = data.get("id", tool_uses_acc[idx]["id"])
                            tool_uses_acc[idx]["name"] = data.get("name", tool_uses_acc[idx]["name"])
                            tool_uses_acc[idx]["input"] = data.get("input", {})
                    elif event_type == "final_message":
                        final_message = data
                    elif event_type == "usage":
                        if isinstance(data, Usage):
                            self._cumulative_usage.input_tokens += data.input_tokens
                            self._cumulative_usage.output_tokens += data.output_tokens
                            self._cumulative_usage.cache_creation_input_tokens += data.cache_creation_input_tokens
                            self._cumulative_usage.cache_read_input_tokens += data.cache_read_input_tokens

            except Exception as e:
                raise TurnError(f"streaming API request failed: {e}") from e

            # Build the assistant message from accumulated data
            if final_message:
                assistant_message = final_message
            else:
                # Reconstruct from accumulated parts
                blocks = []
                if thinking_parts:
                    blocks.append(ContentBlock(type="thinking", thinking="".join(thinking_parts)))
                if text_parts:
                    blocks.append(ContentBlock(type="text", text="".join(text_parts)))
                for idx in sorted(tool_uses_acc.keys()):
                    tc = tool_uses_acc[idx]
                    try:
                        parsed_input = json.loads(tc["arguments"]) if tc["arguments"] else {}
                    except json.JSONDecodeError:
                        parsed_input = tc.get("input", {})
                    blocks.append(ContentBlock(
                        type="tool_use",
                        id=tc["id"],
                        name=tc["name"],
                        input=parsed_input,
                    ))
                assistant_message = Message(role="assistant", content=blocks)

            self.session.push_message(assistant_message)
            assistant_messages.append(assistant_message)

            # Check for tool uses
            pending_tool_uses = assistant_message.tool_uses()
            if not pending_tool_uses:
                break

            # Execute each tool
            manual_compact = False
            for tool_block in pending_tool_uses:
                tool_name = tool_block.name or ""
                tool_use_id = tool_block.id or ""
                tool_input = tool_block.input or {}

                if on_event:
                    on_event("tool_execution_start", {"name": tool_name})

                required_perm = self.tool_registry.required_permission(tool_name)
                outcome = self.permission_policy.authorize(
                    tool_name,
                    json.dumps(tool_input),
                    required_perm,
                    prompter,
                )

                if outcome.allowed:
                    try:
                        result_str = self.tool_executor.execute(tool_name, tool_input)
                        is_error = False
                    except CompactRequested as cr:
                        # Layer 3: manual compact triggered by the compact tool
                        manual_compact = True
                        result_str = json.dumps({"status": "compacting", "focus": cr.focus})
                        is_error = False
                    except ToolError as e:
                        result_str = json.dumps({"error": str(e)})
                        is_error = True
                else:
                    result_str = json.dumps({"error": outcome.reason})
                    is_error = True

                if on_event:
                    on_event("tool_execution_end", {
                        "name": tool_name,
                        "is_error": is_error,
                        "result": result_str,
                    })

                result_message = Message.tool_result(
                    tool_use_id=tool_use_id,
                    tool_name=tool_name,
                    output=result_str,
                    is_error=is_error,
                )
                self.session.push_message(result_message)
                tool_results.append(result_message)

            # Layer 3: if compact tool was called, perform compaction and end the turn
            if manual_compact:
                compact_session(self.session, provider=self.provider)
                break

        summary = TurnSummary(
            assistant_messages=assistant_messages,
            tool_results=tool_results,
            iterations=iterations,
            usage=self._cumulative_usage,
        )
        return summary
