"""Deterministic token-window chunking that never loses its source location.

The chunker is free of model downloads by construction: a token is any
whitespace-delimited run, and a window's budget is measured in
:class:`~tenantchat.api.parsing.tokens.TokenProfile` tokens estimated from
character counts. Two properties make the output golden-fixture stable:

- windows are found by binary search over exact character prefix sums, so the
  same text produces the same windows on every platform and Python version;
- a window never crosses a :class:`~tenantchat.api.parsing.locations.SourceBlock`
  boundary, so a chunk's location is exactly the block its text came from.

The one deliberate budget violation is a single token longer than the whole
window: a word cannot be split, so it is emitted alone.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right

from tenantchat.api.parsing.locations import Chunk, ParsedDocument
from tenantchat.api.parsing.tokens import DEFAULT_TOKEN_PROFILE, TokenProfile

_TOKEN_RE = re.compile(r"\S+")

# Default budgets, matching the prototype chunker's; callers override them per
# document by passing ``chunk_tokens``/``overlap_tokens``.
CHUNK_TOKENS = 650
CHUNK_OVERLAP = 120


def _token_windows(
    text: str,
    *,
    profile: TokenProfile,
    chunk_tokens: int,
    overlap_tokens: int,
) -> list[str]:
    """Split ``text`` into overlapping token windows within the budget."""
    if chunk_tokens < 1:
        raise ValueError(f"chunk_tokens {chunk_tokens} is not positive")
    overlap_tokens = max(0, min(overlap_tokens, chunk_tokens - 1))

    tokens = _TOKEN_RE.findall(text)
    if not tokens:
        return []

    # Character prefix sums over len(token) + 1 (the join separator). A window
    # of tokens[start:end] spans prefix[end] - prefix[start] - 1 characters,
    # so the budget check and the overlap are exact integer comparisons.
    prefix = [0]
    for token in tokens:
        prefix.append(prefix[-1] + len(token) + 1)

    budget_chars = int(chunk_tokens * profile.chars_per_token)
    overlap_chars = int(overlap_tokens * profile.chars_per_token)

    windows: list[str] = []
    start = 0
    count = len(tokens)
    while start < count:
        # Largest window ending within the char budget; the +1 cancels the
        # separator the prefix sums carry.
        end = min(count, max(start + 1, bisect_right(prefix, prefix[start] + budget_chars + 1) - 1))
        windows.append(" ".join(tokens[start:end]))
        if end == count:
            break
        # Next window starts so that the previous window's tail — the overlap
        # region — stays inside the overlap budget.
        next_start = min(end, max(start + 1, bisect_left(prefix, prefix[end] - overlap_chars - 1)))
        start = next_start
    return windows


def chunk_text(
    text: str,
    *,
    profile: TokenProfile = DEFAULT_TOKEN_PROFILE,
    chunk_tokens: int = CHUNK_TOKENS,
    overlap_tokens: int = CHUNK_OVERLAP,
) -> list[str]:
    """Split plain text into overlapping windows; the plain-text chunker.

    Separate from :func:`chunk_document` so a caller that only has text (no
    source structure) can use the same deterministic windows.
    """
    return _token_windows(
        text, profile=profile, chunk_tokens=chunk_tokens, overlap_tokens=overlap_tokens
    )


def chunk_document(
    parsed: ParsedDocument,
    *,
    profile: TokenProfile = DEFAULT_TOKEN_PROFILE,
    chunk_tokens: int = CHUNK_TOKENS,
    overlap_tokens: int = CHUNK_OVERLAP,
) -> list[Chunk]:
    """Chunk every source block, keeping each window's source location."""
    chunks: list[Chunk] = []
    for block in parsed.blocks:
        for window in _token_windows(
            block.text, profile=profile, chunk_tokens=chunk_tokens, overlap_tokens=overlap_tokens
        ):
            chunks.append(Chunk(location=block.location, text=window))
    return chunks
