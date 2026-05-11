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
import os
import signal
import sys
from typing import Sequence

from microclaw import DEFAULT_MODEL
from microclaw.channels.base import Channel
from microclaw.channels.engine import ChannelEngine
from microclaw.channels.transports.queue import QueueChannel
from microclaw.channels.transports.web import WebChannel
from microclaw.channels.transports.wechat import WechatChannel
from microclaw.permissions import PermissionMode
from microclaw.session import load_session_by_reference


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
                engine.handle_message(inbound, sink, use_streaming=use_streaming)
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
