"""Core abstractions for the channels (interaction) layer.

These types are intentionally small and transport-agnostic. A `Channel`
produces `InboundMessage`s and consumes `OutboundEvent`s via a `TurnSink`.

The runtime is unaware of channels — `ChannelEngine` translates runtime
streaming events into sink calls.
"""

from __future__ import annotations

import abc
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterator


# -- Inbound ----------------------------------------------------------------


@dataclass
class InboundMessage:
    """A user message received from a channel.

    Fields beyond `text` are best-effort: a queue transport may only know the
    text and a synthetic id; a WeChat transport would also carry user_id,
    account_id, and a per-conversation reply handle.
    """

    text: str
    id: str = field(default_factory=lambda: f"in_{uuid.uuid4().hex[:12]}")
    user_id: str = "local"
    account_id: str = "default"
    workspace: str | None = None
    session_key: str | None = None
    reply_to: Any = None  # opaque channel-specific handle (e.g. WeChat to_user_id)
    received_at: float = field(default_factory=time.time)
    meta: dict[str, Any] = field(default_factory=dict)


# -- Outbound ---------------------------------------------------------------


class OutboundKind(str, Enum):
    """Kinds of events a TurnSink may receive during one agent turn."""

    TURN_START = "turn_start"
    TEXT_DELTA = "text_delta"
    THINKING_DELTA = "thinking_delta"
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    PERMISSION_REQUEST = "permission_request"
    FINAL = "final"
    ERROR = "error"
    TURN_END = "turn_end"


@dataclass
class OutboundEvent:
    """A normalized event emitted toward a channel during a turn.

    Channels typically consume these via a TurnSink rather than reading
    `OutboundEvent` directly, but transports that persist events (queue,
    web SSE bridge) may serialize them as-is.
    """

    kind: OutboundKind
    data: dict[str, Any] = field(default_factory=dict)
    request_id: str = ""
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "data": self.data,
            "request_id": self.request_id,
            "ts": self.ts,
        }


# -- Sink -------------------------------------------------------------------


class TurnSink(abc.ABC):
    """Render one agent turn back to a channel.

    Channels implement two flavours:

      * StreamingTurnSink — emits each delta as it arrives (terminal, web SSE).
      * BufferedTurnSink  — collects deltas and flushes on `on_turn_end`
                            (WeChat, email, SMS — anything without realtime UI).

    All methods are synchronous to match `ConversationRuntime`'s `on_event`
    callback. Async transports should marshal calls onto their own loop.
    """

    # -- lifecycle --

    def on_turn_start(self, request_id: str, inbound: InboundMessage) -> None:
        return None

    def on_turn_end(self, request_id: str, summary: Any | None = None) -> None:
        return None

    # -- streaming content --

    def on_text_delta(self, text: str) -> None:
        return None

    def on_thinking_delta(self, text: str) -> None:
        return None

    # -- tool lifecycle --

    def on_tool_start(self, name: str, tool_input: dict[str, Any]) -> None:
        return None

    def on_tool_end(
        self,
        name: str,
        tool_input: dict[str, Any],
        is_error: bool,
        result: str,
    ) -> None:
        return None

    # -- terminal events --

    def on_error(self, err: BaseException) -> None:
        return None

    def on_final(self, text: str) -> None:
        """Called once with the final assistant text (after streaming ends).

        For BufferedTurnSink this is typically where the actual delivery
        happens. StreamingTurnSink may treat it as a no-op since deltas
        already covered the output.
        """
        return None

    def close(self) -> None:
        return None


# -- Channel ----------------------------------------------------------------


class Channel(abc.ABC):
    """A transport that ingests user messages and renders agent output.

    Lifecycle:

      1. `serve()` is called by `microclaw serve` and yields `InboundMessage`s.
      2. For each message, the engine calls `open_sink(message)` to obtain a
         per-turn `TurnSink` and runs one agent turn against it.
      3. The channel may keep its own background work running across turns
         (long-poll loops, websockets, etc.) — those belong inside `serve()`.
    """

    #: Stable id used in logs / status (e.g. "queue", "wechat", "web").
    name: str = "channel"

    #: True if `open_sink` returns a streaming-capable sink. Channels that
    #: cannot render token-level updates should set this to False and provide
    #: a BufferedTurnSink so the engine can fall back gracefully.
    supports_streaming: bool = False

    @abc.abstractmethod
    def serve(self) -> Iterator[InboundMessage]:
        """Yield inbound messages until the channel is stopped.

        Implementations should respect interruption (KeyboardInterrupt) and
        any internal stop flags. They may block between messages.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def open_sink(self, message: InboundMessage) -> TurnSink:
        """Return a fresh TurnSink for the given inbound message."""
        raise NotImplementedError

    def stop(self) -> None:
        """Signal the channel to shut down its background work. Optional."""
        return None
