"""Interaction layer for microclaw.

The `channels` package adds a thin gateway above the core runtime so external
surfaces (mobile, WeChat, telegram, web, queue, ...) can drive
`ConversationRuntime` without touching `microclaw.cli` or `microclaw.runtime`.

Layering:

    Channel / Transport       (this package)
        |
        v
    ChannelEngine             (this package — bridges runtime <-> TurnSink)
        |
        v
    ConversationRuntime       (microclaw core — unchanged)
        |
        v
    Tools / Skills            (microclaw core — unchanged)

The core stays streaming-first; each channel decides whether to render output
incrementally (StreamingTurnSink) or buffer per turn (BufferedTurnSink).
"""

from microclaw.channels.base import (
    Channel,
    InboundMessage,
    OutboundEvent,
    OutboundKind,
    TurnSink,
)

__all__ = [
    "Channel",
    "InboundMessage",
    "OutboundEvent",
    "OutboundKind",
    "TurnSink",
]
