"""Concrete `TurnSink` implementations shared across channels.

Two flavours:

  * `StreamingTurnSink` — forwards each delta immediately via an emit callback.
  * `BufferedTurnSink`  — accumulates deltas and flushes a single final payload
                          via a deliver callback when the turn ends.

Both sinks are channel-agnostic: they speak in terms of plain text and tool
metadata, never about transport details. A channel composes a sink with a
callback that knows how to render to its surface (stdout, JSONL queue,
WeChat HTTP API, web SSE, ...).
"""

from __future__ import annotations

import time
from typing import Any, Callable

from microclaw.channels.base import (
    InboundMessage,
    OutboundEvent,
    OutboundKind,
    TurnSink,
)


# -- Streaming sink ---------------------------------------------------------


class StreamingTurnSink(TurnSink):
    """Emit every event as it happens.

    Use for channels with realtime UIs: terminal, web SSE/WebSocket, or any
    transport that can push partial output.

    The `emit` callback receives a normalized `OutboundEvent`. The channel is
    free to translate that into stdout writes, SSE frames, websocket frames,
    etc.
    """

    def __init__(self, emit: Callable[[OutboundEvent], None]):
        self._emit = emit
        self._request_id: str = ""
        self._final_text_parts: list[str] = []

    # -- lifecycle --

    def on_turn_start(self, request_id: str, inbound: InboundMessage) -> None:
        self._request_id = request_id
        self._emit(
            OutboundEvent(
                kind=OutboundKind.TURN_START,
                data={"inbound_id": inbound.id, "user_id": inbound.user_id},
                request_id=request_id,
            )
        )

    def on_turn_end(self, request_id: str, summary: Any | None = None) -> None:
        self._emit(
            OutboundEvent(
                kind=OutboundKind.TURN_END,
                data={},
                request_id=request_id or self._request_id,
            )
        )

    # -- content --

    def on_text_delta(self, text: str) -> None:
        if not text:
            return
        self._final_text_parts.append(text)
        self._emit(
            OutboundEvent(
                kind=OutboundKind.TEXT_DELTA,
                data={"text": text},
                request_id=self._request_id,
            )
        )

    def on_thinking_delta(self, text: str) -> None:
        if not text:
            return
        self._emit(
            OutboundEvent(
                kind=OutboundKind.THINKING_DELTA,
                data={"text": text},
                request_id=self._request_id,
            )
        )

    # -- tools --

    def on_tool_start(self, name: str, tool_input: dict[str, Any]) -> None:
        self._emit(
            OutboundEvent(
                kind=OutboundKind.TOOL_START,
                data={"name": name, "input": tool_input},
                request_id=self._request_id,
            )
        )

    def on_tool_end(
        self,
        name: str,
        tool_input: dict[str, Any],
        is_error: bool,
        result: str,
    ) -> None:
        self._emit(
            OutboundEvent(
                kind=OutboundKind.TOOL_END,
                data={
                    "name": name,
                    "input": tool_input,
                    "is_error": is_error,
                    "result": result,
                },
                request_id=self._request_id,
            )
        )

    # -- terminal --

    def on_final(self, text: str) -> None:
        # Streaming clients already saw every delta; emit FINAL with the
        # accumulated text so consumers that joined late can still render
        # the whole reply.
        joined = text or "".join(self._final_text_parts)
        self._emit(
            OutboundEvent(
                kind=OutboundKind.FINAL,
                data={"text": joined},
                request_id=self._request_id,
            )
        )

    def on_error(self, err: BaseException) -> None:
        self._emit(
            OutboundEvent(
                kind=OutboundKind.ERROR,
                data={"message": str(err), "type": type(err).__name__},
                request_id=self._request_id,
            )
        )


# -- Buffered sink ----------------------------------------------------------


class BufferedTurnSink(TurnSink):
    """Accumulate the turn and deliver one final payload.

    Use for channels that cannot stream tokens (WeChat, email, SMS). The
    sink optionally emits short tool-status checkpoints via `status_emit`
    so users on slow transports still see progress.

    `deliver` is called exactly once with the final assistant text on
    `on_final`. If an error happens before `on_final`, `deliver` is invoked
    with the error notice so the user is never left without a reply.
    """

    def __init__(
        self,
        deliver: Callable[[str], None],
        *,
        status_emit: Callable[[str], None] | None = None,
        include_tool_status: bool = True,
        error_prefix: str = "⚠️ ",
    ):
        self._deliver = deliver
        self._status_emit = status_emit
        self._include_tool_status = include_tool_status
        self._error_prefix = error_prefix
        self._text_parts: list[str] = []
        self._delivered = False
        self._request_id = ""

    # -- lifecycle --

    def on_turn_start(self, request_id: str, inbound: InboundMessage) -> None:
        self._request_id = request_id
        self._text_parts = []
        self._delivered = False

    def on_turn_end(self, request_id: str, summary: Any | None = None) -> None:
        # If on_final never fired (e.g. empty assistant turn) but we still
        # collected some text, flush it now.
        if not self._delivered and self._text_parts:
            self._flush("".join(self._text_parts))

    # -- content --

    def on_text_delta(self, text: str) -> None:
        if text:
            self._text_parts.append(text)

    # -- tools --

    def on_tool_start(self, name: str, tool_input: dict[str, Any]) -> None:
        if self._include_tool_status and self._status_emit:
            try:
                self._status_emit(f"… running {name}")
            except Exception:
                pass

    def on_tool_end(
        self,
        name: str,
        tool_input: dict[str, Any],
        is_error: bool,
        result: str,
    ) -> None:
        if self._include_tool_status and self._status_emit and is_error:
            try:
                self._status_emit(f"{self._error_prefix}{name} failed")
            except Exception:
                pass

    # -- terminal --

    def on_final(self, text: str) -> None:
        body = text if text else "".join(self._text_parts)
        self._flush(body)

    def on_error(self, err: BaseException) -> None:
        notice = f"{self._error_prefix}{type(err).__name__}: {err}"
        # Prefer delivering whatever assistant text we already collected,
        # then append the error so the user has context.
        body = "".join(self._text_parts)
        if body:
            self._flush(f"{body}\n\n{notice}")
        else:
            self._flush(notice)

    # -- internals --

    def _flush(self, text: str) -> None:
        if self._delivered:
            return
        self._delivered = True
        try:
            self._deliver(text or "")
        except Exception:
            # Channels are responsible for their own retries; swallow here so
            # one transport failure doesn't crash the engine.
            pass


# -- Throttled buffered sink ------------------------------------------------


class ThrottledBufferedSink(BufferedTurnSink):
    """Buffered sink that may emit intermediate flushes on a time budget.

    Useful for transports that *can* send multiple messages but cannot
    stream tokens (WeChat, telegram without edit). The sink delivers a
    partial message every `flush_interval` seconds while text is growing,
    and always delivers a final message on `on_final`.

    The simplest correct behaviour is still "deliver once at the end"; this
    subclass is opt-in for channels that benefit from incremental drops.
    """

    def __init__(
        self,
        deliver: Callable[[str], None],
        *,
        flush_interval: float = 6.0,
        min_chars_between_flushes: int = 120,
        status_emit: Callable[[str], None] | None = None,
        include_tool_status: bool = True,
    ):
        super().__init__(
            deliver,
            status_emit=status_emit,
            include_tool_status=include_tool_status,
        )
        self._flush_interval = max(0.5, flush_interval)
        self._min_chars = max(1, min_chars_between_flushes)
        self._last_flush_at = 0.0
        self._last_flush_len = 0

    def on_text_delta(self, text: str) -> None:
        super().on_text_delta(text)
        now = time.time()
        total_len = sum(len(p) for p in self._text_parts)
        if (
            now - self._last_flush_at >= self._flush_interval
            and total_len - self._last_flush_len >= self._min_chars
        ):
            self._partial_flush(total_len, now)

    def _partial_flush(self, total_len: int, now: float) -> None:
        try:
            self._deliver("".join(self._text_parts))
            self._last_flush_at = now
            self._last_flush_len = total_len
        except Exception:
            pass
