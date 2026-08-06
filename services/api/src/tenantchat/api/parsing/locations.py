"""Source locations: where a chunk's text came from, in human terms.

A location is what every acceptance criterion for `RAG-003` rests on: each
chunk remembers the heading path, page number, or HTML anchor that produced
its text, so a retrieved chunk can point an operator — and later a citation
(`RAG-005`) — at the exact place in the source document.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ChunkLocation:
    """One stable, human-readable source location for a run of text.

    ``section_path`` is the heading hierarchy from the document root down to
    the heading the text sits under, empty for a document with no headings.
    ``page`` is a 1-based page number for paginated sources; ``anchor`` is the
    HTML heading id when the source carries one. A location with nothing set
    is the document root itself.
    """

    section_path: tuple[str, ...] = ()
    page: int | None = None
    anchor: str | None = None

    def __post_init__(self) -> None:
        if self.page is not None and self.page < 1:
            raise ValueError(f"page {self.page} is not 1-based")

    def __str__(self) -> str:
        parts = list(self.section_path)
        if self.anchor is not None:
            parts.append(f"#{self.anchor}")
        rendered = " > ".join(parts) or "Document"
        if self.page is not None:
            return f"{rendered} (p. {self.page})"
        return rendered


@dataclass(frozen=True, slots=True)
class SourceBlock:
    """One contiguous run of text that shares a single source location."""

    location: ChunkLocation
    text: str


@dataclass(frozen=True, slots=True)
class Chunk:
    """One chunked window plus the location every window maps back to."""

    location: ChunkLocation
    text: str


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """The adapter output one ingestion job embeds and indexes.

    ``parser_version`` names the adapter that produced the blocks, so
    `OBS-004` can pin an answer's evidence to the exact parser implementation
    the generation was built with.
    """

    title: str
    media_type: str
    parser_version: str
    blocks: tuple[SourceBlock, ...]
