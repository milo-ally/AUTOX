"""Local JSONL queue transport.

A development-friendly channel: messages come in through an `inbox.jsonl`
file and outbound events are appended to `outbox.jsonl`. Anyone — a test
script, a tail -f session, a small web UI — can drive both sides.

Layout (default `~/.microclaw/channels/<name>/`):

    inbox.jsonl    # one JSON object per line, appended by senders
    outbox.jsonl   # appended by the engine via the sink
    cursor.json    # last byte offset consumed from inbox.jsonl

Inbox line format::

    {"text": "hello", "user_id": "milo", "id": "in_..."}

Outbox line format (one OutboundEvent per line)::

    {"kind": "text_delta", "data": {...}, "request_id": "req_...", "ts": ...}

This transport is fully synchronous and uses simple polling. It is the
reference implementation channels should mirror.
"""

from __future__ import annotations

import json
import os
import time
from typing import Iterator

from microclaw.channels.base import (
    Channel,
    InboundMessage,
    OutboundEvent,
    TurnSink,
)
from microclaw.channels.sinks import StreamingTurnSink


DEFAULT_ROOT = os.path.expanduser("~/.microclaw/channels")


class QueueChannel(Channel):
    """File-backed JSONL queue channel."""

    name = "queue"
    supports_streaming = True

    def __init__(
        self,
        *,
        root: str | None = None,
        instance: str = "default",
        poll_interval: float = 0.5,
    ):
        base = root or DEFAULT_ROOT
        self.dir = os.path.join(base, "queue", instance)
        os.makedirs(self.dir, exist_ok=True)
        self.inbox_path = os.path.join(self.dir, "inbox.jsonl")
        self.outbox_path = os.path.join(self.dir, "outbox.jsonl")
        self.cursor_path = os.path.join(self.dir, "cursor.json")
        self.poll_interval = max(0.05, poll_interval)
        self._stopped = False

        # Ensure files exist so tailers don't error.
        for path in (self.inbox_path, self.outbox_path):
            if not os.path.exists(path):
                with open(path, "a", encoding="utf-8"):
                    pass

    # -- public --

    def stop(self) -> None:
        self._stopped = True

    def serve(self) -> Iterator[InboundMessage]:
        """Tail inbox.jsonl and yield InboundMessage records."""
        offset = self._load_cursor()
        while not self._stopped:
            try:
                size = os.path.getsize(self.inbox_path)
            except FileNotFoundError:
                size = 0

            if size > offset:
                with open(self.inbox_path, "r", encoding="utf-8") as f:
                    f.seek(offset)
                    while True:
                        raw_line = f.readline()
                        if not raw_line:
                            break
                        line = raw_line.rstrip("\n")
                        if not line.strip():
                            offset = f.tell()
                            continue
                        msg = self._parse_inbound(line)
                        if msg is None:
                            offset = f.tell()
                            continue
                        # Persist cursor *before* yielding so a crash between
                        # yields cannot cause duplicate consumption.
                        offset = f.tell()
                        self._save_cursor(offset)
                        yield msg
            elif size < offset:
                # The file was truncated/rotated externally — restart.
                offset = 0
                self._save_cursor(0)

            try:
                time.sleep(self.poll_interval)
            except KeyboardInterrupt:
                self._stopped = True
                return

    def open_sink(self, message: InboundMessage) -> TurnSink:
        return StreamingTurnSink(self._append_outbound)

    # -- internals --

    @staticmethod
    def _parse_inbound(line: str) -> InboundMessage | None:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return None
        text = obj.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        return InboundMessage(
            text=text,
            id=str(obj.get("id") or "") or InboundMessage.__dataclass_fields__["id"].default_factory(),
            user_id=str(obj.get("user_id") or "local"),
            account_id=str(obj.get("account_id") or "default"),
            workspace=obj.get("workspace"),
            session_key=obj.get("session_key"),
            meta=obj.get("meta") or {},
        )

    def _append_outbound(self, event: OutboundEvent) -> None:
        line = json.dumps(event.to_dict(), ensure_ascii=False)
        with open(self.outbox_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _load_cursor(self) -> int:
        try:
            with open(self.cursor_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                offset = int(data.get("offset", 0))
                return max(0, offset)
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            return 0

    def _save_cursor(self, offset: int) -> None:
        tmp = self.cursor_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"offset": int(offset)}, f)
        os.replace(tmp, self.cursor_path)


__all__ = ["QueueChannel", "DEFAULT_ROOT"]
