"""FastAPI web channel.

This channel exposes a small browser UI, a JSON chat endpoint, and an SSE
stream for realtime runtime events.

Endpoints:

    GET  /              Minimal browser chat UI
    GET  /api/health    Readiness probe
    POST /api/chat      Submit a user message
    GET  /api/events    Server-Sent Events stream of OutboundEvent objects

The FastAPI server runs in a background thread. `serve()` yields messages from
a thread-safe queue to the regular ChannelEngine loop, keeping the core runtime
unchanged and synchronous.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Iterator

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field
import uvicorn

from microclaw.channels.base import Channel, InboundMessage, OutboundEvent, TurnSink
from microclaw.channels.sinks import StreamingTurnSink


class ChatRequest(BaseModel):
    text: str = Field(min_length=1)
    user_id: str = "web"
    session_key: str | None = None
    workspace: str | None = None
    meta: dict[str, object] = Field(default_factory=dict)


class ChatResponse(BaseModel):
    id: str
    accepted: bool = True


class SessionSwitchRequest(BaseModel):
    session_id: str = Field(min_length=1)


class PermissionResponseRequest(BaseModel):
    request_id: str = Field(min_length=1)
    allowed: bool
    scope: str = "once"


@dataclass
class WebChannelConfig:
    host: str = "127.0.0.1"
    port: int = 8787


class WebChannel(Channel):
    """Browser/FastAPI channel with SSE streaming."""

    name = "web"
    supports_streaming = True

    def __init__(self, *, host: str = "127.0.0.1", port: int = 8787):
        self.config = WebChannelConfig(host=host, port=port)
        self._inbound: queue.Queue[InboundMessage] = queue.Queue()
        self._subscribers: set[queue.Queue[OutboundEvent | None]] = set()
        self._stopped = threading.Event()
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._list_sessions: Callable[[], dict[str, object]] | None = None
        self._new_session: Callable[[], dict[str, object]] | None = None
        self._resume_session: Callable[[str], dict[str, object]] | None = None
        self.permission_responder: Callable[[str, bool, str], dict[str, object]] | None = None
        self.runtime_interrupter: Callable[[], dict[str, object]] | None = None
        self.app = self._build_app()

    # -- Channel ------------------------------------------------------------

    def serve(self) -> Iterator[InboundMessage]:
        self._start_server()
        while not self._stopped.is_set():
            try:
                yield self._inbound.get(timeout=0.2)
            except queue.Empty:
                continue

    def open_sink(self, message: InboundMessage) -> TurnSink:
        return StreamingTurnSink(self._broadcast)

    def set_session_handlers(
        self,
        *,
        list_sessions: Callable[[], dict[str, object]],
        new_session: Callable[[], dict[str, object]],
        resume_session: Callable[[str], dict[str, object]],
    ) -> None:
        self._list_sessions = list_sessions
        self._new_session = new_session
        self._resume_session = resume_session

    def stop(self) -> None:
        self._stopped.set()
        if self._server is not None:
            self._server.should_exit = True
        for subscriber in list(self._subscribers):
            try:
                subscriber.put_nowait(None)
            except queue.Full:
                pass

    # -- FastAPI ------------------------------------------------------------

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="microclaw web channel")

        @app.get("/", response_class=HTMLResponse)
        def index() -> str:
            return _INDEX_HTML

        @app.get("/api/health")
        def health() -> dict[str, object]:
            return {"ok": True, "channel": self.name, "ts": time.time()}

        @app.get("/api/sessions")
        def sessions() -> dict[str, object]:
            if self._list_sessions is None:
                return {"ok": False, "error": "session handlers are not ready", "sessions": []}
            return self._list_sessions()

        @app.post("/api/sessions")
        def sessions_new() -> dict[str, object]:
            if self._new_session is None:
                return {"ok": False, "error": "session handlers are not ready"}
            return self._new_session()

        @app.post("/api/sessions/resume")
        def sessions_resume(req: SessionSwitchRequest) -> dict[str, object]:
            if self._resume_session is None:
                return {"ok": False, "error": "session handlers are not ready"}
            return self._resume_session(req.session_id)

        @app.post("/api/permissions/respond")
        def permissions_respond(req: PermissionResponseRequest) -> dict[str, object]:
            if self.permission_responder is None:
                return {"ok": False, "error": "permission API is not configured"}
            return self.permission_responder(req.request_id, req.allowed, req.scope)

        @app.post("/api/runtime/interrupt")
        def runtime_interrupt() -> dict[str, object]:
            if self.runtime_interrupter is None:
                return {"ok": False, "error": "runtime interrupt API is not configured"}
            return self.runtime_interrupter()

        @app.post("/api/chat", response_model=ChatResponse)
        def chat(req: ChatRequest) -> ChatResponse:
            inbound = InboundMessage(
                id=f"web_{uuid.uuid4().hex[:12]}",
                text=req.text,
                user_id=req.user_id,
                account_id="web",
                workspace=req.workspace,
                session_key=req.session_key,
                meta=dict(req.meta),
            )
            self._inbound.put(inbound)
            return ChatResponse(id=inbound.id)

        @app.get("/api/events")
        def events() -> StreamingResponse:
            subscriber: queue.Queue[OutboundEvent | None] = queue.Queue(maxsize=512)
            self._subscribers.add(subscriber)

            def gen():
                try:
                    yield "event: ready\ndata: {}\n\n"
                    while not self._stopped.is_set():
                        item = subscriber.get()
                        if item is None:
                            break
                        payload = json.dumps(item.to_dict(), ensure_ascii=False)
                        yield f"event: {item.kind.value}\ndata: {payload}\n\n"
                finally:
                    self._subscribers.discard(subscriber)

            return StreamingResponse(gen(), media_type="text/event-stream")

        return app

    def _start_server(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        config = uvicorn.Config(
            self.app,
            host=self.config.host,
            port=self.config.port,
            log_level="info",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()

    # -- events -------------------------------------------------------------

    def _broadcast(self, event: OutboundEvent) -> None:
        for subscriber in list(self._subscribers):
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                self._subscribers.discard(subscriber)


_INDEX_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>microclaw web channel</title>
  <style>
    :root { color-scheme: dark; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    body { margin: 0; background: #0d1117; color: #e6edf3; }
    main { max-width: 900px; margin: 0 auto; padding: 32px 20px; }
    h1 { margin: 0 0 6px; font-size: 28px; }
    p { color: #8b949e; }
    #log { min-height: 420px; border: 1px solid #30363d; border-radius: 14px; padding: 18px; background: #010409; white-space: pre-wrap; line-height: 1.55; overflow-wrap: anywhere; }
    form { display: flex; gap: 10px; margin-top: 16px; }
    textarea { flex: 1; min-height: 54px; resize: vertical; border-radius: 12px; border: 1px solid #30363d; padding: 12px; background: #161b22; color: #e6edf3; font: inherit; }
    button { border: 0; border-radius: 12px; padding: 0 18px; background: #2f81f7; color: white; font-weight: 700; cursor: pointer; }
    button:disabled { opacity: .55; cursor: not-allowed; }
    .event { color: #8b949e; }
    .tool { color: #79c0ff; }
    .error { color: #ff7b72; }
  </style>
</head>
<body>
  <main>
    <h1>microclaw web channel</h1>
    <p>FastAPI + SSE frontend for the interaction layer.</p>
    <section id="log"></section>
    <form id="form">
      <textarea id="input" placeholder="Ask microclaw…"></textarea>
      <button id="send" type="submit">Send</button>
    </form>
  </main>
  <script>
    const log = document.getElementById('log');
    const form = document.getElementById('form');
    const input = document.getElementById('input');
    const send = document.getElementById('send');
    let current = '';

    function append(text, cls) {
      const span = document.createElement('span');
      if (cls) span.className = cls;
      span.textContent = text;
      log.appendChild(span);
      log.scrollTop = log.scrollHeight;
    }

    const es = new EventSource('/api/events');
    es.addEventListener('turn_start', () => { current = ''; append('\\n\\n› microclaw\\n', 'event'); });
    es.addEventListener('text_delta', (ev) => { const msg = JSON.parse(ev.data); current += msg.data.text || ''; append(msg.data.text || ''); });
    es.addEventListener('tool_start', (ev) => { const msg = JSON.parse(ev.data); append(`\\n[tool] ${msg.data.name} running\\n`, 'tool'); });
    es.addEventListener('tool_end', (ev) => { const msg = JSON.parse(ev.data); append(`[tool] ${msg.data.name} ${msg.data.is_error ? 'failed' : 'done'}\\n`, msg.data.is_error ? 'error' : 'tool'); });
    es.addEventListener('error', (ev) => { if (ev.data) append(`\\n${ev.data}\\n`, 'error'); });
    es.addEventListener('turn_end', () => { send.disabled = false; input.disabled = false; input.focus(); });

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const text = input.value.trim();
      if (!text) return;
      append(`\\n\\nYou: ${text}\\n`, 'event');
      input.value = '';
      send.disabled = true;
      input.disabled = true;
      await fetch('/api/chat', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ text, user_id: 'web' }),
      });
    });
  </script>
</body>
</html>
"""


__all__ = ["WebChannel"]
