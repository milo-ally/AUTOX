"""Context compaction — three-layer compression pipeline for infinite sessions.

Layer 1 (micro_compact): Silent, every turn. Replaces old non-read_file tool
    results with "[Previous: used {tool_name}]" placeholders.

Layer 2 (auto_compact): When estimated tokens > THRESHOLD. Saves full
    transcript, asks the model to summarize, replaces all messages with
    a compressed summary.

Layer 3 (manual compact): Triggered by the /compact slash command or the
    compact tool. Same as auto_compact but user-initiated.

Key insight: "The agent can forget strategically and keep working forever."
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from microclaw.providers.types import ContentBlock, Message
from microclaw.session import Session, SessionCompaction


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

THRESHOLD = 50_000          # estimated tokens before auto_compact fires
KEEP_RECENT = 3             # how many tool results to keep in micro_compact
PRESERVE_RESULT_TOOLS = {"read_file"}  # tool results never compacted


def estimate_tokens(messages: list[Message]) -> int:
    """Rough token count: ~4 chars per token."""
    total = 0
    for msg in messages:
        for block in msg.content:
            if block.text:
                total += len(block.text)
            if block.content and isinstance(block.content, str):
                total += len(block.content)
            if block.input:
                total += len(json.dumps(block.input, default=str))
            if block.thinking:
                total += len(block.thinking)
    return total // 4


# ---------------------------------------------------------------------------
# Layer 1: micro_compact — replace old tool results with placeholders
# ---------------------------------------------------------------------------

def micro_compact(messages: list[Message]) -> list[Message]:
    """Replace old tool_result content with short placeholders.

    Keeps the last KEEP_RECENT tool results intact.  Preserves read_file
    outputs because they are reference material; compacting them forces
    the agent to re-read files.
    """
    # Collect indices of all tool_result blocks
    tool_results: list[tuple[int, int, ContentBlock]] = []
    for msg_idx, msg in enumerate(messages):
        if msg.role in ("user", "tool"):
            for part_idx, block in enumerate(msg.content):
                if block.type == "tool_result":
                    tool_results.append((msg_idx, part_idx, block))

    if len(tool_results) <= KEEP_RECENT:
        return messages

    # Build tool_name map from assistant tool_use blocks
    tool_name_map: dict[str, str] = {}
    for msg in messages:
        if msg.role == "assistant":
            for block in msg.content:
                if block.type == "tool_use" and block.id:
                    tool_name_map[block.id] = block.name or "unknown"

    # Clear old results (keep last KEEP_RECENT)
    to_clear = tool_results[:-KEEP_RECENT]
    for _, _, result in to_clear:
        content = result.content
        if not isinstance(content, str) or len(content) <= 100:
            continue
        tool_id = result.tool_use_id or ""
        tool_name = tool_name_map.get(tool_id, "unknown")
        if tool_name in PRESERVE_RESULT_TOOLS:
            continue
        result.content = f"[Previous: used {tool_name}]"

    return messages


# ---------------------------------------------------------------------------
# Layer 2 & 3: auto_compact / manual compact — summarize & replace
# ---------------------------------------------------------------------------

def _save_transcript(messages: list[Message], session_id: str) -> Path:
    """Persist the full message list to a JSONL transcript file."""
    transcript_dir = Path(os.environ.get(
        "MICROCLAW_SESSION_DIR",
        os.path.expanduser("~/.microclaw/transcripts"),
    ))
    transcript_dir.mkdir(parents=True, exist_ok=True)
    path = transcript_dir / f"transcript_{session_id}_{int(time.time())}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(_message_to_compact_dict(msg), ensure_ascii=False, default=str) + "\n")
    return path


def _message_to_compact_dict(msg: Message) -> dict[str, Any]:
    """Convert a Message to a dict suitable for the summarizer."""
    blocks = []
    for block in msg.content:
        d: dict[str, Any] = {"type": block.type}
        if block.text:
            d["text"] = block.text
        if block.name:
            d["name"] = block.name
        if block.input:
            d["input"] = block.input
        if block.content and isinstance(block.content, str):
            d["content"] = block.content[:500]
        blocks.append(d)
    return {"role": msg.role, "content": blocks}


def compact_session(
    session: Session,
    provider: Any = None,
    focus: str = "",
) -> SessionCompaction:
    """Compact the session by summarizing the conversation.

    Args:
        session: The session to compact.
        provider: An LLM provider to generate the summary.
            If None, a simple truncation summary is used.
        focus: Optional hint about what to preserve in the summary.

    Returns:
        SessionCompaction with stats about what was removed.
    """
    messages = session.messages
    removed_count = len(messages)

    # Save transcript before compacting
    transcript_path = _save_transcript(messages, session.session_id)

    # Generate summary
    summary = _generate_summary(messages, provider, focus)

    # Replace all messages with compressed summary
    compact_msg = Message.user_text(
        f"[Conversation compressed. Transcript: {transcript_path}]\n\n{summary}"
    )
    session.messages = [compact_msg]

    # Update compaction metadata
    compaction = SessionCompaction(
        count=(session.compaction.count if session.compaction else 0) + 1,
        removed_message_count=removed_count,
        summary=summary[:500],
    )
    session.compaction = compaction
    session.save()

    return compaction


def _generate_summary(
    messages: list[Message],
    provider: Any,
    focus: str,
) -> str:
    """Generate a conversation summary using the LLM or a fallback."""
    conversation_text = json.dumps(
        [_message_to_compact_dict(m) for m in messages],
        ensure_ascii=False,
        default=str,
    )[-80000:]

    focus_hint = f"\nFocus especially on: {focus}" if focus else ""

    if provider is not None:
        try:
            from microclaw.providers.types import Message as PMessage

            summary_prompt = (
                "Summarize this conversation for continuity. Include: "
                "1) What was accomplished, 2) Current state, "
                "3) Key decisions made, 4) Any pending tasks. "
                "Be concise but preserve critical details."
                f"{focus_hint}\n\n{conversation_text}"
            )
            summary_msg, _ = provider.send_turn(
                messages=[PMessage.user_text(summary_prompt)],
                tools=None,
                system="You are a conversation summarizer. Be concise and precise.",
            )
            return summary_msg.text_content() or "No summary generated."
        except Exception:
            pass  # Fall through to fallback

    # Fallback: simple truncation-based summary
    return _fallback_summary(messages)


def _fallback_summary(messages: list[Message]) -> str:
    """Generate a basic summary without LLM assistance."""
    user_prompts = []
    tool_calls = []
    for msg in messages:
        if msg.role == "user":
            text = msg.text_content()
            if text and not text.startswith("[Conversation compressed"):
                user_prompts.append(text[:200])
        elif msg.role == "assistant":
            for block in msg.content:
                if block.type == "tool_use" and block.name:
                    tool_calls.append(block.name)

    lines = ["[Auto-compacted summary]"]
    if user_prompts:
        lines.append(f"User prompts: {len(user_prompts)}")
        for p in user_prompts[-5:]:
            lines.append(f"  - {p}")
    if tool_calls:
        from collections import Counter
        counts = Counter(tool_calls)
        lines.append(f"Tool calls: {len(tool_calls)} total")
        for name, cnt in counts.most_common(10):
            lines.append(f"  - {name}: {cnt}x")
    return "\n".join(lines)
