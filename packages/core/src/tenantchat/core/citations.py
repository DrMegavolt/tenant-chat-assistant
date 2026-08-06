"""The citation contract: what one grounded claim may publish (`RAG-005`).

A citation names the source a claim was grounded in. The type exists so that
the fields a public client may see are exactly the fields of this value: a
storage key, an indexing fault, or a reviewer's name on a domain record can
never reach a response by omission, the same way ``PublicTenantView`` keeps a
private tenant field off the widget surface.

Citations are **verified by construction**. The graph builds this type only
from the evidence that was actually in the model's context — the exact passage
set the assembled prompt carried — so a fabricated reference, a citation to a
stale (superseded) version, or a chunk owned by another tenant cannot become a
value of this type. The public schema curates further (it drops nothing here;
this type is already the curated subset).

``source_id`` is the identifier the model wrote in its answer (the chunk id
that was admitted to the prompt context), and is what the authorized source
view is addressed by.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Citation:
    """One source a claim in an answer was grounded in, safe to publish.

    ``effective_at`` is the source version's published window start; together
    with ``revision`` it pins the exact document revision the claim came from.
    ``location`` is the passage's place inside the source (a section), so a
    reader can find the passage rather than only the document.
    """

    source_id: str
    title: str
    source_name: str
    location: str
    revision: int
    effective_at: datetime
