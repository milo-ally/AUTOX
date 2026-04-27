"""OpenAI-compatible provider (OpenAI, xAI, DashScope) using the `openai` Python SDK."""

from __future__ import annotations

import json
import os
from typing import Any

import openai

from microclaw.providers.types import (
    ContentBlock,
    Message,
    ToolDefinition,
    Usage,
)


class OpenAIProvider:
    """Wraps the `openai` SDK for any OpenAI-compatible API."""

    def __init__(
        self,
        model: str,
        provider_name: str = "OpenAI",
        api_key_env: str = "OPENAI_API_KEY",
        base_url: str | None = None,
        api_key: str | None = None,
    ):
        self.model = model
        self.provider_name = provider_name
        self._api_key_env = api_key_env

        resolved_key = api_key or os.environ.get(api_key_env)
        if not resolved_key:
            raise ValueError(
                f"Missing {provider_name} credentials. Set {api_key_env} or pass api_key=."
            )

        default_base = {
            "OpenAI": "https://api.openai.com/v1",
            "xAI": "https://api.x.ai/v1",
            "DashScope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        }.get(provider_name, "https://api.openai.com/v1")

        self.client = openai.OpenAI(
            api_key=resolved_key,
            base_url=base_url or default_base,
        )

    def send_turn(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
    ) -> tuple[Message, Usage]:
        """Send a non-streaming request and return (assistant_message, usage)."""
        kwargs = self._build_request_kwargs(messages, tools, system, max_tokens)
        response = self.client.chat.completions.create(**kwargs)
        usage = Usage()
        if response.usage:
            usage.input_tokens = response.usage.prompt_tokens or 0
            usage.output_tokens = response.usage.completion_tokens or 0
        return self._parse_response(response), usage

    def stream_turn(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
    ):
        """Stream a response, yielding (event_type, data) tuples.

        Yields the same event types as AnthropicProvider.stream_turn.
        """
        kwargs = self._build_request_kwargs(messages, tools, system, max_tokens)
        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}

        # Track tool call accumulation across chunks
        tool_calls_acc: dict[int, dict[str, Any]] = {}
        text_started = False
        got_usage = False
        text_parts: list[str] = []

        response = self.client.chat.completions.create(**kwargs)

        for chunk in response:
            if not chunk.choices:
                # Usage-only chunk at the end sometimes
                if hasattr(chunk, "usage") and chunk.usage:
                    got_usage = True
                    yield ("usage", Usage(
                        input_tokens=chunk.usage.prompt_tokens or 0,
                        output_tokens=chunk.usage.completion_tokens or 0,
                    ))
                continue

            choice = chunk.choices[0]
            delta = choice.delta

            reasoning_content = self._extract_reasoning_content(delta)
            if reasoning_content:
                yield ("thinking", reasoning_content)

            # Text content
            content = self._extract_content(delta)
            if content:
                if not text_started:
                    text_started = True
                text_parts.append(content)
                yield ("text", content)

            # Tool calls
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {
                            "id": None,
                            "name": None,
                            "arguments": "",
                        }
                    if tc.id:
                        tool_calls_acc[idx]["id"] = tc.id
                        yield ("tool_use_start", {
                            "id": tc.id,
                            "name": tool_calls_acc[idx].get("name") or "",
                            "index": idx,
                        })
                    if tc.function and tc.function.name:
                        tool_calls_acc[idx]["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        tool_calls_acc[idx]["arguments"] += tc.function.arguments
                        yield ("tool_use_delta", tc.function.arguments)

            # Finish reason
            if choice.finish_reason:
                # Build final tool_use blocks from accumulated data
                for idx, tc_data in sorted(tool_calls_acc.items()):
                    try:
                        parsed_input = json.loads(tc_data["arguments"]) if tc_data["arguments"] else {}
                    except json.JSONDecodeError:
                        parsed_input = {}
                    yield ("tool_use_complete", {
                        "id": tc_data["id"] or f"tool_call_{idx}",
                        "name": tc_data["name"] or "",
                        "input": parsed_input,
                        "index": idx,
                    })
                yield ("done", None)

        # If we never got a finish_reason, emit done anyway
        yield ("done", None)

        # Fallback: if the API never returned usage (some OpenAI-compatible providers don't support stream_options), estimate from text lengths
        if not got_usage:
            input_chars = sum(
                len(json.dumps(m, default=str)) for m in self._convert_messages(messages, system)
            )
            output_chars = sum(len(t) for t in text_parts)
            output_chars += sum(len(tc_data.get("arguments", "")) for tc_data in tool_calls_acc.values())
            yield ("usage", Usage(
                input_tokens=input_chars // 4,
                output_tokens=output_chars // 4,
            ))

    def _build_request_kwargs(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        system: str | None,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        api_messages = self._convert_messages(messages, system)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": api_messages,
            "max_tokens": max_tokens or 16000,
        }
        if tools:
            kwargs["tools"] = [self._convert_tool(t) for t in tools]
            kwargs["tool_choice"] = "auto"
        return kwargs

    def _convert_messages(
        self,
        messages: list[Message],
        system: str | None = None,
    ) -> list[dict[str, Any]]:
        """Convert our Message objects to the OpenAI chat format."""
        result = []
        if system:
            result.append({"role": "system", "content": system})

        for msg in messages:
            if msg.role == "user":
                text_parts = []
                tool_results = []
                for block in msg.content:
                    if block.type == "text" and block.text:
                        text_parts.append(block.text)
                    elif block.type == "tool_result":
                        tool_results.append(block)
                if text_parts:
                    result.append({"role": "user", "content": "\n".join(text_parts)})
                for tr in tool_results:
                    result.append({
                        "role": "tool",
                        "tool_call_id": tr.tool_use_id,
                        "content": tr.content or "",
                    })
            elif msg.role == "assistant":
                text_parts = [b.text for b in msg.content if b.type == "text" and b.text]
                thinking_parts = [b.thinking for b in msg.content if b.type == "thinking" and b.thinking]
                tool_uses = [b for b in msg.content if b.type == "tool_use"]
                content = "\n".join(text_parts) if text_parts else None
                msg_dict: dict[str, Any] = {"role": "assistant"}
                if content:
                    msg_dict["content"] = content
                if thinking_parts and tool_uses:
                    # DeepSeek thinking/tool-use turns need reasoning_content echoed back
                    # to continue the same turn, but we avoid carrying old reasoning into
                    # later user turns by only forwarding it on assistant tool-call messages.
                    msg_dict["reasoning_content"] = "\n".join(thinking_parts)
                if tool_uses:
                    msg_dict["tool_calls"] = [
                        {
                            "id": tu.id,
                            "type": "function",
                            "function": {
                                "name": tu.name,
                                "arguments": json.dumps(tu.input or {}),
                            },
                        }
                        for tu in tool_uses
                    ]
                result.append(msg_dict)
            elif msg.role == "tool":
                for block in msg.content:
                    if block.type == "tool_result":
                        result.append({
                            "role": "tool",
                            "tool_call_id": block.tool_use_id,
                            "content": block.content or "",
                        })
        return result

    @staticmethod
    def _convert_tool(tool: ToolDefinition) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }

    @staticmethod
    def _parse_response(response) -> Message:
        """Parse an OpenAI SDK response into our Message type."""
        choice = response.choices[0]
        blocks = []

        reasoning_content = OpenAIProvider._extract_reasoning_content(choice.message)
        if reasoning_content:
            blocks.append(ContentBlock(type="thinking", thinking=reasoning_content))

        if choice.message.content:
            blocks.append(ContentBlock(type="text", text=choice.message.content))

        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                try:
                    parsed_input = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    parsed_input = {}
                blocks.append(ContentBlock(
                    type="tool_use",
                    id=tc.id,
                    name=tc.function.name,
                    input=parsed_input,
                ))

        return Message(role="assistant", content=blocks)

    @staticmethod
    def _extract_content(obj: Any) -> str | None:
        """Read content from SDK objects, including providers with extra fields."""
        content = getattr(obj, "content", None)
        if content:
            return content
        extra = getattr(obj, "model_extra", None) or {}
        return extra.get("content")

    @staticmethod
    def _extract_reasoning_content(obj: Any) -> str | None:
        """Read provider-specific reasoning content from SDK objects."""
        reasoning = getattr(obj, "reasoning_content", None)
        if reasoning:
            return reasoning
        extra = getattr(obj, "model_extra", None) or {}
        return extra.get("reasoning_content")
