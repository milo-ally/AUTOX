"""Provider detection, routing, and shared abstractions."""

from __future__ import annotations

import os
from enum import Enum

# Model alias mapping (short names → full model ids)
MODEL_ALIASES: dict[str, str] = {
    "opus": "claude-opus-4-20250515",
    "sonnet": "claude-sonnet-4-20250514",
    "haiku": "claude-haiku-4-20250514",
    "4o": "gpt-4o",
    "4o-mini": "gpt-4o-mini",
    "o3": "o3",
    "o4-mini": "o4-mini",
    "grok": "grok-3",
    "grok-mini": "grok-3-mini",
    "qwq": "qwen-qwq-32b",
    "deepseek": "deepseek-chat",
    "deepseek-chat": "deepseek-chat",
    "deepseek-reasoner": "deepseek-reasoner",
}


class ProviderKind(Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    XAI = "xai"
    DASHSCOPE = "dashscope"


def resolve_model_alias(model: str) -> str:
    """Expand short model aliases to full model ids."""
    return MODEL_ALIASES.get(model, model)


def detect_provider_kind(model: str) -> ProviderKind:
    """Detect which provider to use based on the model name."""
    lowered = model.lower()

    # xAI models
    if lowered.startswith("grok"):
        return ProviderKind.XAI

    # DashScope / Qwen models
    if lowered.startswith("qwen") or lowered.startswith("qwq"):
        return ProviderKind.DASHSCOPE

    # OpenAI models
    if any(lowered.startswith(p) for p in ("gpt-", "gpt4", "o1-", "o1", "o3", "o4", "chatgpt")):
        return ProviderKind.OPENAI
    if "/" in model:
        prefix = model.split("/")[0].lower()
        if prefix == "openai":
            return ProviderKind.OPENAI
        if prefix == "xai":
            return ProviderKind.XAI
        if prefix in ("qwen", "dashscope"):
            return ProviderKind.DASHSCOPE

    # DeepSeek models — route based on available credentials
    if lowered.startswith("deepseek"):
        if os.environ.get("OPENAI_API_KEY"):
            return ProviderKind.OPENAI
        if os.environ.get("ANTHROPIC_API_KEY"):
            return ProviderKind.ANTHROPIC
        return ProviderKind.OPENAI  # default to OpenAI-compatible

    # Default: pick provider based on available credentials
    # If OPENAI_API_KEY is set, use OpenAI; otherwise Anthropic
    if os.environ.get("OPENAI_API_KEY"):
        return ProviderKind.OPENAI
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ProviderKind.ANTHROPIC

    # Last resort: Anthropic
    return ProviderKind.ANTHROPIC


def create_provider(model: str):
    """Create the appropriate provider client for a model."""
    from microclaw.providers.anthropic import AnthropicProvider
    from microclaw.providers.openai import OpenAIProvider

    resolved = resolve_model_alias(model)
    kind = detect_provider_kind(resolved)

    if kind == ProviderKind.ANTHROPIC:
        return AnthropicProvider(model=resolved)
    elif kind == ProviderKind.XAI:
        return OpenAIProvider(
            model=resolved,
            provider_name="xAI",
            api_key_env="XAI_API_KEY",
            base_url=os.environ.get("XAI_BASE_URL", "https://api.x.ai/v1"),
        )
    elif kind == ProviderKind.DASHSCOPE:
        return OpenAIProvider(
            model=resolved,
            provider_name="DashScope",
            api_key_env="DASHSCOPE_API_KEY",
            base_url=os.environ.get(
                "DASHSCOPE_BASE_URL",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ),
        )
    else:
        return OpenAIProvider(
            model=resolved,
            provider_name="OpenAI",
            api_key_env="OPENAI_API_KEY",
            base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        )
