"""Anthropic provider using the official `anthropic` Python SDK."""

from __future__ import annotations

import os
from typing import Any

import anthropic

from microclaw.providers.types import (
    ContentBlock,
    Message,
    ToolDefinition,
    Usage,
)


class AnthropicProvider:
    """Wraps the `anthropic` SDK to send messages and stream responses."""

    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None):
        self.model = model
        api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError(
                "Missing Anthropic credentials. Set ANTHROPIC_API_KEY or pass api_key=."
            )
        base_url = base_url or os.environ.get("ANTHROPIC_BASE_URL")
        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        self.client = anthropic.Anthropic(**client_kwargs)

    def send_turn(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
    ) -> tuple[Message, Usage]:
        """Send a non-streaming request and return (assistant_message, usage)."""
        kwargs = self._build_request_kwargs(messages, tools, system, max_tokens)
        response = self.client.messages.create(**kwargs)
        usage = Usage(
            input_tokens=getattr(response.usage, 'input_tokens', 0) or 0,
            output_tokens=getattr(response.usage, 'output_tokens', 0) or 0,
            cache_creation_input_tokens=getattr(response.usage, 'cache_creation_input_tokens', 0) or 0,
            cache_read_input_tokens=getattr(response.usage, 'cache_read_input_tokens', 0) or 0,
        )
        return self._parse_response(response), usage

    def stream_turn(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
    ):
        """Stream a response, yielding (event_type, data) tuples.

        Yields:
            ("text", str) — text delta
            ("thinking", str) — thinking delta
            ("tool_use_start", dict) — {id, name, index}
            ("tool_use_delta", str) — partial JSON delta
            ("usage", Usage) — usage info
            ("done", None) — stream complete
        """
        kwargs = self._build_request_kwargs(messages, tools, system, max_tokens)

        with self.client.messages.stream(**kwargs) as stream:
            for event in stream:
                if event.type == "content_block_start":
                    block = event.content_block
                    if block.type == "text":
                        pass  # text block started
                    elif block.type == "tool_use":
                        yield ("tool_use_start", {
                            "id": block.id,
                            "name": block.name,
                            "index": event.index,
                        })
                    elif block.type == "thinking":
                        pass
                elif event.type == "content_block_delta":
                    delta = event.delta
                    if delta.type == "text_delta":
                        yield ("text", delta.text)
                    elif delta.type == "input_json_delta":
                        yield ("tool_use_delta", delta.partial_json)
                    elif delta.type == "thinking_delta":
                        yield ("thinking", delta.thinking)
                elif event.type == "message_delta":
                    pass  # usage tracked via final_message below
                elif event.type == "message_stop":
                    yield ("done", None)

        # Get final message for complete tool_use inputs and full usage
        final = stream.get_final_message()
        final_usage = Usage(
            input_tokens=getattr(final.usage, 'input_tokens', 0) or 0,
            output_tokens=getattr(final.usage, 'output_tokens', 0) or 0,
            cache_creation_input_tokens=getattr(final.usage, 'cache_creation_input_tokens', 0) or 0,
            cache_read_input_tokens=getattr(final.usage, 'cache_read_input_tokens', 0) or 0,
        )
        yield ("usage", final_usage)
        yield ("final_message", self._parse_response(final))

    def _build_request_kwargs(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        system: str | None,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        api_messages = self._convert_messages(messages)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": api_messages,
            "max_tokens": max_tokens or 16000,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [self._convert_tool(t) for t in tools]
        return kwargs

    def _convert_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Convert our Message objects to the Anthropic SDK format."""
        result = []
        for msg in messages:
            if msg.role == "user":
                content = []
                for block in msg.content:
                    if block.type == "text":
                        content.append({"type": "text", "text": block.text or ""})
                    elif block.type == "tool_result":
                        content_block: dict[str, Any] = {
                            "type": "tool_result",
                            "tool_use_id": block.tool_use_id,
                        }
                        if block.is_error:
                            content_block["is_error"] = True
                        content_block["content"] = block.content or ""
                        content.append(content_block)
                if not content:
                    content = [{"type": "text", "text": ""}]
                result.append({"role": "user", "content": content})
            elif msg.role == "assistant":
                content = []
                for block in msg.content:
                    if block.type == "text":
                        content.append({"type": "text", "text": block.text or ""})
                    elif block.type == "thinking":
                        content.append({
                            "type": "thinking",
                            "thinking": block.thinking or "",
                        })
                    elif block.type == "tool_use":
                        content.append({
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input or {},
                        })
                result.append({"role": "assistant", "content": content})
            elif msg.role == "tool":
                # Anthropic expects tool_result inside user messages
                # Convert standalone tool messages into user content blocks
                content = []
                for block in msg.content:
                    if block.type == "tool_result":
                        tb: dict[str, Any] = {
                            "type": "tool_result",
                            "tool_use_id": block.tool_use_id,
                        }
                        if block.is_error:
                            tb["is_error"] = True
                        tb["content"] = block.content or ""
                        content.append(tb)
                if content:
                    # Merge into previous user message if possible, else add new
                    if result and result[-1]["role"] == "user":
                        result[-1]["content"].extend(content)
                    else:
                        result.append({"role": "user", "content": content})
        return result

    @staticmethod
    def _convert_tool(tool: ToolDefinition) -> dict[str, Any]:
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
        }

    @staticmethod
    def _parse_response(response) -> Message:
        """Parse an Anthropic SDK response into our Message type."""
        # Also store usage on the response for external access
        _ = getattr(response, 'usage', None)  # accessed in send_turn/stream_turn
        blocks = []
        for block in response.content:
            if block.type == "text":
                blocks.append(ContentBlock(type="text", text=block.text))
            elif block.type == "tool_use":
                blocks.append(ContentBlock(
                    type="tool_use",
                    id=block.id,
                    name=block.name,
                    input=block.input,
                ))
            elif block.type == "thinking":
                blocks.append(ContentBlock(
                    type="thinking",
                    thinking=getattr(block, "thinking", ""),
                    signature=getattr(block, "signature", None),
                ))
        return Message(role="assistant", content=blocks)
