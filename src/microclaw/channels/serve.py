"""`microclaw serve` — run microclaw as a long-lived gateway for a channel.

This entry is intentionally separate from the interactive REPL (`microclaw`):
the REPL is for humans at a terminal; `serve` is for non-TTY surfaces driven
through `microclaw.channels`.

Usage::

    microclaw serve
    microclaw serve --channel queue --instance default
    microclaw serve --channel queue --permission-mode workspace-write
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import uuid
from typing import Sequence

from microclaw import DEFAULT_MODEL, __version__
from microclaw.channels.base import Channel, OutboundEvent, OutboundKind
from microclaw.channels.engine import ChannelEngine
from microclaw.channels.transports.queue import QueueChannel
from microclaw.channels.transports.web import WebChannel
from microclaw.channels.transports.wechat import WechatChannel
from microclaw.permissions import PermissionMode, PermissionOutcome
from microclaw.providers import create_provider, resolve_model_alias
from microclaw.session import (
    list_sessions,
    load_session_by_reference,
    new_session,
)
from microclaw.tools.tasks import TaskSystem


# -- channel factory --------------------------------------------------------


def _build_channel(name: str, args: argparse.Namespace) -> Channel:
    if name == "queue":
        return QueueChannel(
            root=args.queue_root,
            instance=args.instance,
            poll_interval=args.poll_interval,
        )
    if name == "web":
        return WebChannel(host=args.host, port=args.port)
    if name == "wechat":
        return WechatChannel(
            account_id=args.instance,
            base_url=args.wechat_base_url,
            token=args.wechat_token,
            poll_timeout_ms=args.wechat_poll_timeout_ms,
            state_root=args.wechat_state_root,
            throttled=not args.wechat_no_throttle,
        )
    raise SystemExit(f"unknown --channel: {name!r} (supported: web, queue, wechat)")


# -- argparse ---------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="microclaw serve",
        description="Run microclaw as a gateway behind an interaction channel.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Channels:\n"
            "  web       FastAPI + browser UI + SSE streaming (default)\n"
            "  queue     Local JSONL inbox/outbox for tests and integrations\n"
            "  wechat    Text-only WeChat iLink long-poll channel\n"
            "\n"
            "Examples:\n"
            "  microclaw serve\n"
            "  microclaw serve --host 0.0.0.0 --port 8787\n"
            "  microclaw serve --channel queue --instance smoke\n"
            "  microclaw wechat login\n"
            "  microclaw serve --channel wechat\n"
        ),
    )
    p.add_argument(
        "--channel",
        default="web",
        help="Channel transport to bind: web | queue | wechat (default: web).",
    )
    p.add_argument(
        "--instance",
        default="default",
        help="Channel instance/account id (queue/wechat: state subdirectory; default: default).",
    )
    p.add_argument(
        "--queue-root",
        default=None,
        help="Override the queue channel root directory "
        "(default: ~/.microclaw/channels).",
    )
    p.add_argument(
        "--poll-interval",
        type=float,
        default=0.5,
        help="Queue channel polling interval in seconds (default: 0.5).",
    )
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="Web channel host (default: 127.0.0.1).",
    )
    p.add_argument(
        "--port",
        type=int,
        default=8787,
        help="Web channel port (default: 8787).",
    )
    p.add_argument(
        "--wechat-base-url",
        default=os.environ.get("MICROCLAW_WECHAT_BASE_URL", "http://127.0.0.1:48080/"),
        help="WeChat iLink base URL. Can also use MICROCLAW_WECHAT_BASE_URL.",
    )
    p.add_argument(
        "--wechat-token",
        default=os.environ.get("MICROCLAW_WECHAT_TOKEN"),
        help="WeChat bot token. Can also use MICROCLAW_WECHAT_TOKEN.",
    )
    p.add_argument(
        "--wechat-poll-timeout-ms",
        type=int,
        default=35_000,
        help="WeChat getupdates long-poll timeout in milliseconds.",
    )
    p.add_argument(
        "--wechat-state-root",
        default=None,
        help="Root directory for WeChat channel state "
        "(default: ~/.microclaw/channels/wechat).",
    )
    p.add_argument(
        "--wechat-no-throttle",
        action="store_true",
        help="Disable throttled buffered WeChat replies; send final text only.",
    )
    p.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="Model to use for the channel session.",
    )
    p.add_argument(
        "--permission-mode",
        default="read-only",
        help="Permission mode: read-only | workspace-write | danger-full-access. "
        "Channel turns deny any tool that needs more than this.",
    )
    p.add_argument(
        "--allowed-tools",
        default=None,
        help="Comma-separated allowlist of tool names for served turns (default: all tools).",
    )
    p.add_argument(
        "--workspace",
        default=None,
        help="Working directory the agent should treat as the workspace. "
        "Defaults to the current directory.",
    )
    p.add_argument(
        "--resume",
        default=None,
        help="Resume a session by id, 'latest', or file path.",
    )
    p.add_argument(
        "--session-dir",
        default=None,
        help="Override MICROCLAW_SESSION_DIR for this serve.",
    )
    p.add_argument(
        "--no-stream",
        action="store_true",
        help="Force non-streaming runtime (still delivered through the sink).",
    )
    return p


# -- main -------------------------------------------------------------------


class WebApprovalPrompter:
    def __init__(self, mode: PermissionMode, channel: WebChannel, cancel_checker=None):
        self.mode = mode
        self.escalated = False
        self._channel = channel
        self._cancel_checker = cancel_checker
        self._pending: dict[str, dict[str, object]] = {}
        self._lock = threading.Lock()

    def ask(
        self,
        tool_name: str,
        tool_input: str,
        required: PermissionMode,
        current: PermissionMode,
    ) -> PermissionOutcome:
        request_id = f"perm_{uuid.uuid4().hex[:12]}"
        event = threading.Event()
        pending = {"event": event, "allowed": False, "scope": "once"}
        with self._lock:
            self._pending[request_id] = pending
        self._channel._broadcast(
            OutboundEvent(
                kind=OutboundKind.PERMISSION_REQUEST,
                data={
                    "request_id": request_id,
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "required_permission": required.value,
                    "current_permission": current.value,
                },
            )
        )
        while not event.wait(0.1):
            if self._cancel_checker and self._cancel_checker():
                with self._lock:
                    self._pending.pop(request_id, None)
                raise KeyboardInterrupt
        with self._lock:
            pending = self._pending.pop(request_id, pending)
        if not bool(pending.get("allowed")):
            return PermissionOutcome.deny("user denied")
        if pending.get("scope") == "session":
            self.mode = required
            self.escalated = True
        return PermissionOutcome.allow()

    def respond(self, request_id: str, allowed: bool, scope: str) -> dict[str, object]:
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None:
                return {"ok": False, "error": "permission request is not pending"}
            pending["allowed"] = allowed
            pending["scope"] = scope
            event = pending.get("event")
        if isinstance(event, threading.Event):
            event.set()
        return {"ok": True}


def _handle_served_slash_command(
    text: str,
    *,
    engine: ChannelEngine,
    sink,
    inbound,
) -> bool:
    trimmed = text.strip()
    if not trimmed.startswith("/"):
        return False

    request_id = f"slash_{inbound.id}"
    parts = trimmed.split(maxsplit=1)
    command = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    if command == "/help":
        response = (
            "# microclaw commands\n\n"
            "## General\n"
            "- `/help` Show this help\n"
            "- `/status` Show session status\n"
            "- `/model [name]` Show or switch model\n"
            "- `/permissions [mode]` Show or set permissions\n"
            "- `/version` Show microclaw version\n\n"
            "## Session\n"
            "- `/compact [focus]` Compact conversation history\n"
            "- `/clear` Clear conversation history\n"
            "- `/cost` Show token usage and estimated cost\n"
            "- `/history` Show recent prompts\n"
            "- `/session` List managed sessions\n"
            "- `/resume <id|latest|path>` Resume a session\n"
            "- `/export [path]` Export session to JSON\n\n"
            "## Task/team/skills\n"
            "- `/tasks ...` Manage tasks\n"
            "- `/skills ...` Manage skills\n"
            "- `/team ...` Manage agent team"
        )
    elif command == "/status":
        response = (
            f"# Status\n\n"
            f"- **model**: `{engine.model}`\n"
            f"- **session**: `{engine.session.session_id}`\n"
            f"- **messages**: `{len(engine.session.messages)}`\n"
            f"- **permission mode**: `{engine.permission_mode}`"
        )
    elif command == "/model":
        if args.strip():
            new_model = resolve_model_alias(args.strip())
            engine.model = new_model
            engine.provider = create_provider(new_model)
            engine.session.model = new_model
            engine.team_manager.model = new_model
            response = f"Model set to `{new_model}`."
        else:
            response = f"Current model: `{engine.model}`"
    elif command == "/permissions":
        if args.strip():
            try:
                engine.permission_mode = PermissionMode.from_str(args.strip())
                response = f"Permissions set to `{engine.permission_mode}`."
            except ValueError as e:
                response = str(e)
        else:
            response = f"Current permissions: `{engine.permission_mode}`"
    elif command == "/clear":
        engine.session = new_session()
        engine.session.model = engine.model
        engine.session.save()
        response = "Session conversation history cleared."
    elif command == "/cost":
        usage = engine._cumulative_usage
        team_usage = engine.team_manager.aggregate_usage()
        response = (
            "# Cost\n\n"
            f"- **input tokens**: `{usage.input_tokens + team_usage.input_tokens:,}`\n"
            f"- **output tokens**: `{usage.output_tokens + team_usage.output_tokens:,}`\n"
            f"- **total tokens**: `{usage.total_tokens() + team_usage.total_tokens():,}`\n"
            f"- **model**: `{engine.model}`"
        )
    elif command == "/history":
        entries = getattr(engine.session, "prompt_history", [])[-20:]
        response = "\n".join(f"- {entry.text}" for entry in entries) or "No prompt history."
    elif command == "/session":
        sessions = list_sessions()
        response = (
            "No managed sessions."
            if not sessions
            else "\n".join(
                f"- `{s['id']}` msgs={s['message_count']} model={s.get('model', '?')}"
                for s in sessions
            )
        )
    elif command == "/resume":
        if not args.strip():
            response = "Usage: `/resume <session-id|latest|path>`"
        else:
            try:
                session = load_session_by_reference(args.strip())
                engine.session = session
                engine.model = session.model or engine.model
                engine.provider = create_provider(engine.model)
                engine.team_manager.model = engine.model
                response = f"Resumed session `{session.session_id}`."
            except FileNotFoundError as e:
                response = str(e)
    elif command == "/export":
        path = args.strip() or f"microclaw-export-{engine.session.session_id}.json"
        response = _export_served_session(engine, path)
    elif command == "/tasks":
        response = _handle_tasks_command(args.strip())
    elif command == "/skills":
        response = _handle_skills_command(engine, args.strip())
    elif command == "/team":
        response = _handle_team_command(engine, args.strip())
    elif command == "/version":
        response = f"microclaw `{__version__}`"
    elif command in {"/exit", "/quit"}:
        response = "The web channel stays running. Stop `microclaw serve` from the terminal to exit."
    else:
        response = f"Unknown command: `{command}`. Use `/help` to see available commands."

    sink.on_turn_start(request_id, inbound)
    sink.on_final(response)
    sink.on_turn_end(request_id, None)
    return True


def _handle_tasks_command(args: str) -> str:
    parts = args.split()
    subcommand = parts[0].lower() if parts else "list"
    ts = TaskSystem()
    if subcommand in ("list", "ls", ""):
        result = ts.list_tasks()
        summary = result.get("summary", [])
        counts = result.get("counts", {})
        if not summary:
            return "No tasks."
        return (
            f"Tasks: {counts.get('pending', 0)} pending, "
            f"{counts.get('in_progress', 0)} in_progress, "
            f"{counts.get('completed', 0)} completed\n\n"
            + "\n".join(f"- {line}" for line in summary)
        )
    if subcommand in ("add", "create", "new"):
        if len(parts) < 2:
            return "Usage: `/tasks add <subject> [--status pending|in_progress|completed] [--priority high|medium|low]`"
        subject: list[str] = []
        status = "pending"
        priority = "medium"
        skip_next = False
        for i, part in enumerate(parts[1:], start=1):
            if skip_next:
                skip_next = False
                continue
            if part == "--status" and i + 1 < len(parts):
                status = parts[i + 1]
                skip_next = True
            elif part == "--priority" and i + 1 < len(parts):
                priority = parts[i + 1]
                skip_next = True
            else:
                subject.append(part)
        result = ts.create_task({"subject": " ".join(subject), "status": status, "priority": priority})
        task = result.get("task", {})
        return f"Created task #{task.get('id')}: {task.get('subject')}"
    if subcommand in ("update", "done", "start", "stop"):
        if len(parts) < 2:
            return "Usage: `/tasks update <id> [--status ...] [--priority ...]`"
        try:
            task_id = int(parts[1])
        except ValueError:
            return "Task ID must be an integer."
        update_data: dict[str, object] = {"task_id": task_id}
        if subcommand == "done":
            update_data["status"] = "completed"
        elif subcommand == "start":
            update_data["status"] = "in_progress"
        elif subcommand == "stop":
            update_data["status"] = "pending"
        else:
            skip_next = False
            for i, part in enumerate(parts[2:], start=2):
                if skip_next:
                    skip_next = False
                    continue
                if part == "--status" and i + 1 < len(parts):
                    update_data["status"] = parts[i + 1]
                    skip_next = True
                elif part == "--priority" and i + 1 < len(parts):
                    update_data["priority"] = parts[i + 1]
                    skip_next = True
        result = ts.update_task(update_data)
        task = result.get("task", {})
        return f"Updated task #{task.get('id')}: {task.get('subject')}"
    if subcommand in ("get", "show", "info"):
        if len(parts) < 2:
            return "Usage: `/tasks get <id>`"
        try:
            task = ts.get_task(int(parts[1])).get("task", {})
        except ValueError:
            return "Task ID must be an integer."
        return json.dumps(task, ensure_ascii=False, indent=2)
    if subcommand in ("delete", "rm", "del", "remove"):
        if len(parts) < 2:
            return "Usage: `/tasks delete <id>`"
        try:
            ts.delete_task(int(parts[1]))
        except ValueError:
            return "Task ID must be an integer."
        return f"Deleted task #{parts[1]}"
    if subcommand == "clear":
        result = ts.clear_tasks()
        return f"Cleared {result.get('tasks_cleared', 0)} tasks."
    return "Usage: `/tasks [list|add|update|get|delete|done|start|stop|clear]`"


def _handle_skills_command(engine: ChannelEngine, args: str) -> str:
    parts = args.split()
    subcommand = parts[0].lower() if parts else "list"
    if subcommand in ("list", "ls"):
        names = engine.skill_loader.available_names()
        if not names:
            return "No skills found."
        rows = []
        for name in names:
            skill = engine.skill_loader.get(name)
            if skill is None:
                continue
            active = " active" if name in engine.active_skills else ""
            rows.append(f"- `{skill.name}`{active}: {skill.description or 'No description'}")
        return "\n".join(rows)
    if subcommand in ("show", "active"):
        return ", ".join(engine.active_skills) if engine.active_skills else "No active skills."
    if subcommand == "reload":
        engine.skill_loader.reload()
        engine.active_skills = [name for name in engine.active_skills if engine.skill_loader.get(name)]
        engine.system_prompt = engine._build_system_prompt()
        return f"Reloaded {len(engine.skill_loader.available_names())} skills."
    if subcommand in ("use", "enable", "add"):
        if len(parts) < 2:
            return "Usage: `/skills use <name>`"
        name = parts[1]
        if not engine.skill_loader.get(name):
            return f"Unknown skill: `{name}`"
        if name not in engine.active_skills:
            engine.active_skills.append(name)
            engine.system_prompt = engine._build_system_prompt()
        return f"Skill activated: `{name}`"
    if subcommand in ("drop", "disable", "remove", "rm"):
        if len(parts) < 2:
            return "Usage: `/skills drop <name>`"
        name = parts[1]
        if name in engine.active_skills:
            engine.active_skills.remove(name)
            engine.system_prompt = engine._build_system_prompt()
            return f"Skill deactivated: `{name}`"
        return f"Skill not active: `{name}`"
    return "Usage: `/skills [list|show|reload|use <name>|drop <name>]`"


def _handle_team_command(engine: ChannelEngine, args: str) -> str:
    parts = args.split()
    subcommand = parts[0].lower() if parts else "list"
    tm = engine.team_manager
    if subcommand in ("list", "ls", ""):
        return tm.list_all()
    if subcommand == "spawn":
        if len(parts) < 3:
            return "Usage: `/team spawn <name> <role> --prompt <prompt>`"
        prompt = " ".join(parts[3:])
        if "--prompt" in parts:
            prompt = " ".join(parts[parts.index("--prompt") + 1:])
        if not prompt:
            return "Usage: `/team spawn <name> <role> --prompt <prompt>`"
        return tm.spawn(parts[1], parts[2], prompt)
    if subcommand == "shutdown":
        return "Usage: `/team shutdown <name>`" if len(parts) < 2 else tm.request_shutdown(parts[1])
    if subcommand == "inbox":
        messages = tm.read_lead_inbox()
        if not messages:
            return "Inbox is empty."
        return "\n".join(f"- [{m.get('type', 'message')}] from {m.get('from', '?')}: {m.get('content', '')}" for m in messages)
    if subcommand in ("approve", "reject"):
        if len(parts) < 2:
            return f"Usage: `/team {subcommand} <request_id>`"
        return tm.review_plan(parts[1], subcommand == "approve", " ".join(parts[2:]))
    if subcommand == "plans":
        pending = tm.get_pending_plans()
        if not pending:
            return "No pending plan requests."
        return "\n".join(f"- `{p.get('request_id', '?')}` from {p.get('from', '?')}: {p.get('plan', '')}" for p in pending)
    if subcommand == "cost":
        usage = tm.aggregate_usage()
        return f"Team input tokens: `{usage.input_tokens:,}`\n\nTeam output tokens: `{usage.output_tokens:,}`"
    return "Usage: `/team [list|spawn|shutdown|inbox|approve|reject|plans|cost]`"


def _export_served_session(engine: ChannelEngine, path: str) -> str:
    messages = []
    for msg in engine.session.messages:
        messages.append(
            {
                "role": msg.role,
                "content": [
                    {
                        key: value
                        for key, value in {
                            "type": block.type,
                            "text": block.text,
                            "name": block.name,
                            "input": block.input,
                            "content": block.content,
                        }.items()
                        if value
                    }
                    for block in msg.content
                ],
            }
        )
    export_data = {
        "session_id": engine.session.session_id,
        "model": engine.model,
        "messages": messages,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(export_data, f, indent=2, ensure_ascii=False)
    return f"Session exported to `{path}`."

def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)

    try:
        permission_mode = PermissionMode.from_str(args.permission_mode)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    allowed_tools = None
    if args.allowed_tools:
        allowed_tools = {t.strip() for t in args.allowed_tools.split(",") if t.strip()}

    if args.session_dir:
        os.environ["MICROCLAW_SESSION_DIR"] = os.path.expanduser(args.session_dir)
        import microclaw.session as _session_mod
        _session_mod.DEFAULT_SESSION_DIR = os.environ["MICROCLAW_SESSION_DIR"]

    if args.workspace:
        workspace = os.path.abspath(os.path.expanduser(args.workspace))
        os.chdir(workspace)
    else:
        workspace = os.getcwd()

    session = None
    if args.resume:
        try:
            session = load_session_by_reference(args.resume)
        except FileNotFoundError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    engine = ChannelEngine(
        model=args.model,
        permission_mode=permission_mode,
        allowed_tools=allowed_tools,
        session=session,
        workspace_dir=workspace,
    )
    channel = _build_channel(args.channel, args)
    web_prompter = None

    if isinstance(channel, WebChannel):
        web_prompter = WebApprovalPrompter(
            permission_mode,
            channel,
            cancel_checker=engine._cancel_event.is_set,
        )
        channel.permission_responder = web_prompter.respond
        channel.runtime_interrupter = engine.cancel_current_turn

        def _web_session_messages() -> list[dict[str, object]]:
            messages: list[dict[str, object]] = []
            for msg in engine.session.messages:
                text = msg.text_content()
                if not text:
                    continue
                messages.append({
                    "id": f"{msg.role}_{len(messages)}",
                    "role": msg.role,
                    "content": text,
                    "timestamp": engine.session.updated_at_ms,
                })
            return messages

        def _session_payload() -> dict[str, object]:
            return {
                "ok": True,
                "current_session_id": engine.session.session_id,
                "current_messages": _web_session_messages(),
                "sessions": list_sessions(),
            }

        def _new_web_session() -> dict[str, object]:
            engine.session = new_session()
            engine.session.model = engine.model
            engine.session.save()
            return _session_payload()

        def _resume_web_session(session_id: str) -> dict[str, object]:
            try:
                session = load_session_by_reference(session_id)
            except FileNotFoundError as e:
                return {"ok": False, "error": str(e), "sessions": list_sessions()}
            engine.session = session
            engine.model = session.model or engine.model
            engine.provider = create_provider(engine.model)
            engine.team_manager.model = engine.model
            return _session_payload()

        channel.set_session_handlers(
            list_sessions=_session_payload,
            new_session=_new_web_session,
            resume_session=_resume_web_session,
        )

    use_streaming = (not args.no_stream) and channel.supports_streaming

    # Graceful shutdown via SIGINT/SIGTERM.
    def _handle_signal(signum, frame):
        channel.stop()
    try:
        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
    except (ValueError, AttributeError):
        pass  # not always available (e.g. non-main thread, Windows)

    print(
        f"microclaw serve: channel={channel.name} instance={args.instance} "
        f"model={engine.model} mode={permission_mode} streaming={use_streaming} "
        f"workspace={workspace} session={engine.session.session_id}",
        flush=True,
    )

    try:
        for inbound in channel.serve():
            sink = channel.open_sink(inbound)
            try:
                if _handle_served_slash_command(
                    inbound.text,
                    engine=engine,
                    sink=sink,
                    inbound=inbound,
                ):
                    continue
                requested_permission_mode = None
                requested_permission_mode_raw = inbound.meta.get("permission_mode")
                if isinstance(requested_permission_mode_raw, str):
                    try:
                        requested_permission_mode = PermissionMode.from_str(
                            requested_permission_mode_raw
                        )
                    except ValueError:
                        requested_permission_mode = None
                engine.handle_message(
                    inbound,
                    sink,
                    use_streaming=use_streaming,
                    permission_mode=requested_permission_mode,
                    prompter=web_prompter if isinstance(channel, WebChannel) else None,
                )
            except KeyboardInterrupt:
                break
            except Exception as e:
                # ChannelEngine already routed the error through the sink;
                # log here for the operator and keep serving.
                print(f"[serve] turn failed: {e}", file=sys.stderr, flush=True)
            finally:
                try:
                    sink.close()
                except Exception:
                    pass
    except KeyboardInterrupt:
        pass
    finally:
        channel.stop()

    print("microclaw serve: stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
