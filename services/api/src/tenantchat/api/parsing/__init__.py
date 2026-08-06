"""Versioned document parsing and chunking for the ingestion pipeline.

``parse_document`` dispatches on the version's media type to the registered
format adapter; ``chunk_document`` turns the adapter's structured blocks into
deterministic token windows that each remember their source location.
``CHUNKER_VERSION`` and each adapter's ``version`` are the identifiers the
generation record (`OBS-004`) pins an answer's evidence to — bumping one is a
deliberate contract change, exactly as with the embedding model.
"""

from __future__ import annotations

from tenantchat.api.parsing.adapters import (
    DocxParser,
    HtmlParser,
    MarkdownParser,
    ParserAdapter,
    PdfParser,
    TextParser,
)
from tenantchat.api.parsing.chunker import (
    CHUNK_OVERLAP,
    CHUNK_TOKENS,
    chunk_document,
    chunk_text,
)
from tenantchat.api.parsing.injection import (
    ContentSafetyReport,
    InjectionSignal,
    content_fingerprint,
    scan_for_injection,
)
from tenantchat.api.parsing.locations import Chunk, ChunkLocation, ParsedDocument, SourceBlock
from tenantchat.api.parsing.scan import MAX_DOCUMENT_BYTES, scan_bytes, scan_text
from tenantchat.api.parsing.tokens import (
    DEFAULT_TOKEN_PROFILE,
    TOKEN_PROFILES,
    TokenProfile,
    count_tokens,
    profile_for,
)
from tenantchat.core.errors import ValidationError

CHUNKER_VERSION = "token-window.v2"

__all__ = [
    "CHUNKER_VERSION",
    "CHUNK_OVERLAP",
    "CHUNK_TOKENS",
    "DEFAULT_TOKEN_PROFILE",
    "MAX_DOCUMENT_BYTES",
    "SUPPORTED_MEDIA_TYPES",
    "TOKEN_PROFILES",
    "Chunk",
    "ChunkLocation",
    "ContentSafetyReport",
    "InjectionSignal",
    "ParsedDocument",
    "SourceBlock",
    "TokenProfile",
    "chunk_document",
    "chunk_text",
    "content_fingerprint",
    "count_tokens",
    "normalize_media_type",
    "parse_document",
    "parser_version_for",
    "profile_for",
    "scan_bytes",
    "scan_for_injection",
    "scan_text",
]

_ADAPTERS: tuple[ParserAdapter, ...] = (
    MarkdownParser(),
    HtmlParser(),
    TextParser(),
    PdfParser(),
    DocxParser(),
)
_ADAPTER_BY_MEDIA_TYPE: dict[str, ParserAdapter] = {
    media_type: adapter for adapter in _ADAPTERS for media_type in adapter.media_types
}

# The media types the pipeline can parse. The upload route accepts exactly
# this set, so a format that would become a broken ingestion job is refused
# at the door.
SUPPORTED_MEDIA_TYPES = frozenset(_ADAPTER_BY_MEDIA_TYPE)


def normalize_media_type(media_type: str) -> str:
    """The canonical media type, ignoring parameters and case."""
    return media_type.split(";", 1)[0].strip().lower()


def parser_version_for(media_type: str) -> str:
    """The version of the adapter that would parse ``media_type``.

    Raises:
        ValidationError: no adapter supports the media type.
    """
    adapter = _ADAPTER_BY_MEDIA_TYPE.get(normalize_media_type(media_type))
    if adapter is None:
        raise ValidationError(detail=f"media type {media_type!r} is not supported")
    return adapter.version


def parse_document(content: bytes, *, media_type: str, title: str) -> ParsedDocument:
    """Parse one document's bytes through the adapter for its media type.

    Raises:
        ValidationError: the media type is unsupported, or the content failed
            scanning or parsing (oversized, corrupt, encrypted, empty).
    """
    normalized = normalize_media_type(media_type)
    adapter = _ADAPTER_BY_MEDIA_TYPE.get(normalized)
    if adapter is None:
        raise ValidationError(detail=f"media type {media_type!r} is not supported")
    return adapter.parse(content, title=title, media_type=normalized)
