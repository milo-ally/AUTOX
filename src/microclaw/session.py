"""Session persistence — JSONL-based session save/load."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from microclaw.providers.types import ContentBlock, Message


SESSION_VERSION = 1

# Default session storage directory — can be overridden via MICROCLAW_SESSION_DIR env var
DEFAULT_SESSION_DIR = os.environ.get(
    "MICROCLAW_SESSION_DIR",
    os.path.expanduser("~/.microclaw/sessions"),
)


def _ensure_session_dir() -> Path:
    """Ensure the session directory exists and return it."""
    path = Path(DEFAULT_SESSION_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _generate_session_id() -> str:
    """Generate a unique session ID."""
    return uuid.uuid4().hex[:12]


@dataclass
class SessionCompaction:
    count: int = 0
    removed_message_count: int = 0
    summary: str = ""


@dataclass
class PromptHistoryEntry:
    timestamp_ms: int
    text: str


@dataclass
class Session:
    """Persisted conversational state for the runtime and CLI session manager.

    OpenClaw-style lifecycle fields:
      - session_started_at_ms: when the current sessionId began (daily reset uses this)
      - last_interaction_at_ms: last user/channel interaction (idle reset uses this)
      - idle_reset_minutes: new session after N minutes of inactivity (0 = disabled)
      - daily_reset: if True, new session at 4:00 AM local time
    """

    version: int = SESSION_VERSION
    session_id: str = field(default_factory=_generate_session_id)
    created_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    updated_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    messages: list[Message] = field(default_factory=list)
    compaction: SessionCompaction | None = None
    model: str | None = None
    prompt_history: list[PromptHistoryEntry] = field(default_factory=list)
    persistence_path: str | None = None
    # OpenClaw-style lifecycle
    session_started_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    last_interaction_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    idle_reset_minutes: int = 0  # 0 = disabled
    daily_reset: bool = True

    def push_message(self, message: Message) -> None:
        """Add a message and persist it."""
        self.touch()
        self.messages.append(message)
        self._append_persisted_message(message)

    def push_user_text(self, text: str) -> None:
        """Add a user text message."""
        self.push_message(Message.user_text(text))

    def push_prompt_entry(self, text: str) -> None:
        """Record a prompt in the history."""
        entry = PromptHistoryEntry(
            timestamp_ms=int(time.time() * 1000),
            text=text,
        )
        self.prompt_history.append(entry)
        self._append_persisted_record("prompt_entry", {
            "timestamp_ms": entry.timestamp_ms,
            "text": entry.text,
        })

    def touch(self) -> None:
        """Update the modification timestamp."""
        self.updated_at_ms = int(time.time() * 1000)

    def save(self) -> None:
        """Save the full session to its persistence path."""
        if not self.persistence_path:
            return
        path = Path(self.persistence_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for line in self._render_jsonl():
                f.write(line + "\n")

    def _render_jsonl(self) -> list[str]:
        """Render the session as JSONL lines."""
        lines = []

        # Meta record
        meta: dict[str, Any] = {
            "type": "session_meta",
            "version": self.version,
            "session_id": self.session_id,
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
        }
        if self.model:
            meta["model"] = self.model
        if self.compaction:
            meta["compaction"] = {
                "count": self.compaction.count,
                "removed_message_count": self.compaction.removed_message_count,
                "summary": self.compaction.summary,
            }
        lines.append(json.dumps(meta, ensure_ascii=False))

        # Prompt history
        for entry in self.prompt_history:
            lines.append(json.dumps({
                "type": "prompt_entry",
                "timestamp_ms": entry.timestamp_ms,
                "text": entry.text,
            }, ensure_ascii=False))

        # Messages
        for msg in self.messages:
            lines.append(json.dumps(
                _message_to_dict(msg),
                ensure_ascii=False,
            ))

        return lines

    def _append_persisted_message(self, message: Message) -> None:
        """Append a single message to the JSONL file."""
        if not self.persistence_path:
            return
        path = Path(self.persistence_path)
        if not path.exists() or path.stat().st_size == 0:
            self.save()
            return
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(_message_to_dict(message), ensure_ascii=False) + "\n")

    def _append_persisted_record(self, record_type: str, data: dict[str, Any]) -> None:
        """Append a single record to the JSONL file."""
        if not self.persistence_path:
            return
        path = Path(self.persistence_path)
        if not path.exists() or path.stat().st_size == 0:
            self.save()
            return
        data["type"] = record_type
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")


def new_session() -> Session:
    """Create a new session with a persistence path."""
    session = Session()
    session_dir = _ensure_session_dir()
    session.persistence_path = str(session_dir / f"{session.session_id}.jsonl")
    return session


def load_session(path: str) -> Session:
    """Load a session from a JSONL file."""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    # Try JSONL format first
    session = _parse_jsonl(content)
    session.persistence_path = path
    return session


def load_session_by_reference(ref: str) -> Session:
    """Load a session by reference ('latest', 'last', 'recent', or session ID/path)."""
    if ref in ("latest", "last", "recent"):
        return _load_latest_session()

    # Check if it's a path
    if os.path.isfile(ref):
        return load_session(ref)

    # Check if it's a session ID
    session_dir = _ensure_session_dir()
    candidate = session_dir / f"{ref}.jsonl"
    if candidate.exists():
        return load_session(str(candidate))

    # Try with .json extension (legacy)
    candidate_json = session_dir / f"{ref}.json"
    if candidate_json.exists():
        return load_session(str(candidate_json))

    raise FileNotFoundError(f"session not found: {ref}")


def _load_latest_session() -> Session:
    """Load the most recently modified session."""
    session_dir = _ensure_session_dir()
    session_files = sorted(
        session_dir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not session_files:
        raise FileNotFoundError("no managed sessions found")
    return load_session(str(session_files[0]))


def delete_session(session_id: str) -> bool:
    """Delete a session file by session ID. Returns True if deleted."""
    session_dir = _ensure_session_dir()
    candidate = session_dir / f"{session_id}.jsonl"
    if candidate.is_file():
        candidate.unlink()
        return True
    return False


def list_sessions() -> list[dict[str, Any]]:
    """List all managed sessions."""
    session_dir = _ensure_session_dir()
    sessions = []
    for path in sorted(session_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            session = load_session(str(path))
            sessions.append({
                "id": session.session_id,
                "path": str(path),
                "updated_at_ms": session.updated_at_ms,
                "message_count": len(session.messages),
                "model": session.model,
            })
        except Exception:
            continue
    return sessions


def _parse_jsonl(content: str) -> Session:
    """Parse JSONL content into a Session object."""
    session = Session()
    messages = []
    prompt_history = []

    for line in content.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        record_type = record.get("type", "")

        if record_type == "session_meta":
            session.version = record.get("version", SESSION_VERSION)
            session.session_id = record.get("session_id", session.session_id)
            session.created_at_ms = record.get("created_at_ms", session.created_at_ms)
            session.updated_at_ms = record.get("updated_at_ms", session.updated_at_ms)
            session.model = record.get("model")
            if "compaction" in record:
                c = record["compaction"]
                session.compaction = SessionCompaction(
                    count=c.get("count", 0),
                    removed_message_count=c.get("removed_message_count", 0),
                    summary=c.get("summary", ""),
                )
        elif record_type == "prompt_entry":
            prompt_history.append(PromptHistoryEntry(
                timestamp_ms=record.get("timestamp_ms", 0),
                text=record.get("text", ""),
            ))
        else:
            # Treat as a message record
            msg = _dict_to_message(record)
            if msg:
                messages.append(msg)

    session.messages = messages
    session.prompt_history = prompt_history
    return session


def _message_to_dict(msg: Message) -> dict[str, Any]:
    """Convert a Message to a JSON-serializable dict."""
    blocks = []
    for block in msg.content:
        d: dict[str, Any] = {"type": block.type}
        if block.text is not None:
            d["text"] = block.text
        if block.id is not None:
            d["id"] = block.id
        if block.name is not None:
            d["name"] = block.name
        if block.input is not None:
            d["input"] = block.input
        if block.tool_use_id is not None:
            d["tool_use_id"] = block.tool_use_id
        if block.content is not None:
            d["content"] = block.content
        if block.is_error:
            d["is_error"] = True
        if block.thinking is not None:
            d["thinking"] = block.thinking
        blocks.append(d)

    return {
        "role": msg.role,
        "content": blocks,
    }


def _dict_to_message(data: dict[str, Any]) -> Message | None:
    """Convert a dict back to a Message."""
    role = data.get("role")
    if not role:
        return None

    blocks = []
    for block_data in data.get("content", []):
        if isinstance(block_data, str):
            blocks.append(ContentBlock(type="text", text=block_data))
        elif isinstance(block_data, dict):
            blocks.append(ContentBlock(
                type=block_data.get("type", "text"),
                text=block_data.get("text"),
                id=block_data.get("id"),
                name=block_data.get("name"),
                input=block_data.get("input"),
                tool_use_id=block_data.get("tool_use_id"),
                content=block_data.get("content"),
                is_error=block_data.get("is_error", False),
                thinking=block_data.get("thinking"),
                signature=block_data.get("signature"),
            ))

    return Message(role=role, content=blocks)
