"""Small, explicit model capability registry used for safe output defaults.

The registry is intentionally conservative: unknown models keep the 4096-token
default until their owner supplies a verified limit.  Known official model
families can opt into their published output ceiling without forcing users to
copy that value into every model profile.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelCapability:
    max_output_tokens: int
    context_window: int | None = None
    source: str = 'manual-safe-default'


MODEL_CAPABILITIES: dict[str, ModelCapability] = {
    # OpenAI's GPT-5.6 model page publishes a 128k maximum output and 1.05m
    # context window.  Keep the exact id and the family alias for catalog rows.
    'gpt-5.6-sol': ModelCapability(128_000, 1_050_000, 'official-openai'),
    'gpt-5.6': ModelCapability(128_000, 1_050_000, 'official-openai'),
    'gpt-5': ModelCapability(128_000, 1_050_000, 'official-openai'),
}
SAFE_DEFAULT = ModelCapability(4_096)


def capability_for(model_id: str, context_window: int | None = None) -> ModelCapability:
    key = (model_id or '').strip().lower()
    capability = MODEL_CAPABILITIES.get(key)
    if capability is None:
        capability = next((v for k, v in MODEL_CAPABILITIES.items() if key.startswith(k + '-')), SAFE_DEFAULT)
    if context_window is not None and context_window > 0:
        return ModelCapability(min(capability.max_output_tokens, context_window), context_window, capability.source)
    return capability


def output_limit(model_id: str, context_window: int | None = None) -> int:
    return capability_for(model_id, context_window).max_output_tokens
