"""Provider presets for user-connected AI models, and model listing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from krellbot.ai.http import HttpTransport

Format = Literal["openai_compatible", "anthropic_messages"]
SignIn = Literal["none", "openrouter", "chatgpt"]
TokenLimitField = Literal["max_tokens", "max_completion_tokens"]

ANTHROPIC_VERSION = "2023-06-01"
_MODELS_TIMEOUT = 15.0
_MAX_MODELS = 1000
_MAX_MODEL_ID = 256


@dataclass(frozen=True)
class ProviderPreset:
    id: str
    label: str
    format: Format
    # Empty for custom presets: the user supplies the endpoint.
    base_url: str
    signin: SignIn
    lists_models: bool
    # Only OpenAI's own API wants ``max_completion_tokens``; compatible servers expect ``max_tokens``.
    token_limit_field: TokenLimitField = "max_tokens"

    @property
    def custom(self) -> bool:
        return not self.base_url


PRESETS: dict[str, ProviderPreset] = {
    p.id: p
    for p in (
        ProviderPreset("anthropic", "Anthropic", "anthropic_messages", "https://api.anthropic.com", "none", True),
        ProviderPreset(
            "openai",
            "OpenAI",
            "openai_compatible",
            "https://api.openai.com/v1",
            "chatgpt",
            True,
            token_limit_field="max_completion_tokens",
        ),
        ProviderPreset(
            "gemini",
            "Google Gemini",
            "openai_compatible",
            "https://generativelanguage.googleapis.com/v1beta/openai",
            "none",
            True,
        ),
        ProviderPreset("xai", "xAI", "openai_compatible", "https://api.x.ai/v1", "none", True),
        ProviderPreset("mistral", "Mistral", "openai_compatible", "https://api.mistral.ai/v1", "none", True),
        ProviderPreset("deepseek", "DeepSeek", "openai_compatible", "https://api.deepseek.com/v1", "none", True),
        ProviderPreset(
            "openrouter", "OpenRouter", "openai_compatible", "https://openrouter.ai/api/v1", "openrouter", True
        ),
        ProviderPreset("custom_openai", "Custom OpenAI-compatible endpoint", "openai_compatible", "", "none", True),
        # Anthropic-compatible gateways rarely serve a model list; the user types the model name.
        ProviderPreset(
            "custom_anthropic", "Custom Anthropic-compatible endpoint", "anthropic_messages", "", "none", False
        ),
    )
}


def fetch_model_ids(
    format: Format,
    base_url: str,
    credential: str | None,
    transport: HttpTransport,
) -> list[str]:
    """Ask the provider which models it serves. Raises ``TransportError``."""

    if format == "anthropic_messages":
        url = base_url.rstrip("/").removesuffix("/v1") + "/v1/models?limit=1000"
        headers = {"anthropic-version": ANTHROPIC_VERSION}
        if credential:
            headers["x-api-key"] = credential
    else:
        url = base_url.rstrip("/") + "/models"
        headers = {"authorization": f"Bearer {credential}"} if credential else {}
    return parse_model_ids(transport.get_json(url, headers, _MODELS_TIMEOUT))


def parse_model_ids(payload: Any) -> list[str]:
    """Model ids from an OpenAI- or Anthropic-shaped list. Untrusted input: bounded and deduplicated."""

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in data:
        model_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(model_id, str):
            continue
        model_id = model_id.strip()
        if not model_id or len(model_id) > _MAX_MODEL_ID or not model_id.isprintable() or model_id in seen:
            continue
        seen.add(model_id)
        out.append(model_id)
        if len(out) >= _MAX_MODELS:
            break
    return out
