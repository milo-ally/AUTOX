"""Shared types for provider message/event abstraction."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def accumulate(self, other: Usage) -> None:
        """Add another Usage's counts into this one."""
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_creation_input_tokens += other.cache_creation_input_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens
        

@dataclass
class ContentBlock:
    """A content block within a message — text, tool_use, or tool_result."""

    type: str  # "text", "tool_use", "tool_result", "thinking"
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: Any = None  # JSON-decoded dict for tool_use
    tool_use_id: str | None = None
    content: Any = None  # str or list for tool_result
    is_error: bool = False
    thinking: str | None = None
    signature: str | None = None


@dataclass
class Message:
    """A single message in the conversation."""

    role: str  # "user", "assistant", "tool"
    content: list[ContentBlock] = field(default_factory=list)

    @staticmethod
    def user_text(text: str) -> Message:
        return Message(
            role="user",
            content=[ContentBlock(type="text", text=text)],
        )

    @staticmethod
    def assistant_text(text: str) -> Message:
        return Message(
            role="assistant",
            content=[ContentBlock(type="text", text=text)],
        )

    @staticmethod
    def tool_result(
        tool_use_id: str,
        tool_name: str,
        output: str,
        is_error: bool = False,
    ) -> Message:
        return Message(
            role="tool",
            content=[
                ContentBlock(
                    type="tool_result",
                    tool_use_id=tool_use_id,
                    name=tool_name,
                    content=output,
                    is_error=is_error,
                )
            ],
        )

    def text_content(self) -> str:
        """Extract all text from text blocks."""
        parts = []
        for block in self.content:
            if block.type == "text" and block.text:
                parts.append(block.text)
        return "\n".join(parts)

    def tool_uses(self) -> list[ContentBlock]:
        """Return all tool_use blocks."""
        return [b for b in self.content if b.type == "tool_use"]


@dataclass
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class TurnSummary:
    assistant_messages: list[Message] = field(default_factory=list)
    tool_results: list[Message] = field(default_factory=list)
    iterations: int = 0
    usage: Usage = field(default_factory=Usage)
