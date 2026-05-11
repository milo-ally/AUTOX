"""Channel transport implementations.

Each transport plugs into the `Channel` abstraction. The bundled `queue`
transport is a local file-based JSONL inbox/outbox useful for development
and tests. Real transports (WeChat, telegram, web SSE, ...) live as
sibling modules.
"""
