"""Model-aware token counting for chunk budgets.

Embedding models do not publish tokenizer vocabularies, and the chunker must
stay deterministic without downloading a model, so a budget is estimated from
a chars-per-token ratio rather than a real tokenizer. The estimate is a
documented bound — the 4-chars-per-token rule of thumb, tuned per model
family — not an exact count, and the chunker keeps every window within it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

DEFAULT_CHARS_PER_TOKEN = 4.0


@dataclass(frozen=True, slots=True)
class TokenProfile:
    """The chars-per-token ratio estimating one embedding model family."""

    name: str
    chars_per_token: float

    def __post_init__(self) -> None:
        if self.chars_per_token <= 0:
            raise ValueError(f"chars_per_token {self.chars_per_token} is not positive")

    def tokens_for(self, chars: int) -> int:
        """Estimated token count for ``chars`` characters."""
        if chars < 0:
            raise ValueError(f"chars {chars} is negative")
        return math.ceil(chars / self.chars_per_token)


DEFAULT_TOKEN_PROFILE = TokenProfile(name="default", chars_per_token=DEFAULT_CHARS_PER_TOKEN)

# Ratios for known embedding model families. Unknown families fall back to the
# default rather than failing the job; an entry here is the tuning point when
# a model's true tokenizer behavior is measured.
TOKEN_PROFILES: dict[str, TokenProfile] = {
    "default": DEFAULT_TOKEN_PROFILE,
    "qwen3-embedding": TokenProfile(
        name="qwen3-embedding", chars_per_token=DEFAULT_CHARS_PER_TOKEN
    ),
}


def profile_for(model_family: str) -> TokenProfile:
    """The token profile for one embedding model family, default if unknown."""
    return TOKEN_PROFILES.get(model_family, DEFAULT_TOKEN_PROFILE)


def count_tokens(text: str, profile: TokenProfile = DEFAULT_TOKEN_PROFILE) -> int:
    """Estimated token count of ``text`` under ``profile``."""
    return profile.tokens_for(len(text))
