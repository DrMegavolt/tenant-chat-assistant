"""Tenant-isolated object storage for uploaded knowledge content.

The prototype's ingestion endpoint took a caller-supplied filesystem path and
read it (``services/ingestion/app.py``), which is the vulnerability `RAG-002`
exists to remove. Here the caller never names a path at all: the upload route
validates the bytes and the storage adapter derives a **server-owned key** from
values the server already holds — tenant, source, external key, and checksum.
The key is the unit of isolation: every key begins with
``tenants/{tenant_id}/`` and the disk adapter refuses to resolve anything
outside that prefix, so even a buggy caller cannot read or write another
tenant's blob, let alone a container file.

The key is deliberately derived rather than accepted: a storage key is a
control record on ``knowledge_document_versions.storage_key``, and a value the
caller could shape is a path traversal waiting for a parser that is too
forgiving.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from tenantchat.core.errors import NotFoundError, ValidationError

# Slugs stay within the charset of the tenant and domain identifiers the rest
# of the system already treats as safe.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# A validated upload filename: no separators, no dots that would start a
# traversal, no control characters, bounded.
_FILENAME_RE = re.compile(r"^[^/\\\x00-\x1f]{1,255}$")

_TENANT_PREFIX = "tenants/"


def validated_filename(raw: str) -> str:
    """Accept a plain filename or reject a path-shaped one.

    Raises:
        ValidationError: the value contains a path separator, a ``..``
            component, control characters, or is otherwise not a single
            filename. The rejection message never echoes the value: the value
            is attacker-controlled and could itself be a traversal probe.
    """
    candidate = raw.strip()
    if not _FILENAME_RE.fullmatch(candidate):
        raise ValidationError(detail="filename must be a single plain name")
    if candidate in {".", ".."} or candidate.startswith(".."):
        raise ValidationError(detail="filename must not contain dot segments")
    return candidate


def slugify(part: str, *, max_length: int = 128) -> str:
    """Lowercase a free-text part into a safe key component.

    The upload filename is caller-controlled text, so it must pass through this
    before it can influence a storage key: separators, whitespace runs, and
    characters outside the slug charset are replaced, and the result is
    re-checked against the slug regex so a pathological input cannot smuggle a
    ``..`` through.

    Raises:
        ValidationError: the part slugified to nothing (an empty or
            all-separator value has no safe identity).
    """
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", part.strip().lower())
    slug = slug.strip(".-")
    slug = slug[:max_length].rstrip(".-")
    if not _SLUG_RE.fullmatch(slug):
        raise ValidationError(detail="value has no safe slug form")
    return slug


@dataclass(frozen=True, slots=True)
class StorageKey:
    """A parsed, server-derived object-storage key.

    The string form is what ``knowledge_document_versions.storage_key``
    persists. :meth:`parse` validates the whole key and :attr:`path` returns
    the tenant-relative components, so adapters can resolve it without trusting
    any part of it.

    Raises:
        ValidationError: ``parse`` rejects a key that is not a well-formed
            tenant-scoped key — including any ``..``, absolute, or
            non-``tenants/{tenant}/``-prefixed value.
    """

    tenant_id: str
    path: tuple[str, ...]

    @classmethod
    def build(
        cls,
        *,
        tenant_id: str,
        source_id: uuid.UUID,
        external_key: str,
        checksum: str,
    ) -> StorageKey:
        """Derive the key for one uploaded document revision.

        Every component is validated or slugified; ``checksum`` is the
        already-validated SHA-256 hex. The key identifies *content*: a re-upload
        of identical bytes derives the identical key, which is what lets a
        retried or duplicate ingestion find the same blob instead of piling up
        copies.
        """
        if not _TENANT_RE.fullmatch(tenant_id):
            raise ValidationError(detail=f"tenant id {tenant_id!r} is not a valid slug")
        return cls(
            tenant_id=tenant_id,
            path=(
                "tenants",
                tenant_id,
                str(source_id),
                slugify(external_key),
                checksum,
            ),
        )

    @classmethod
    def parse(cls, raw: str) -> StorageKey:
        """Parse a stored key back into its tenant-scoped components.

        Raises:
            ValidationError: the key is malformed or not tenant-scoped.
        """
        if not raw.startswith(_TENANT_PREFIX):
            raise ValidationError(detail="storage key must start with the tenants prefix")
        parts = tuple(raw.split("/"))
        if len(parts) < 4:
            raise ValidationError(detail="storage key has too few components")
        tenant_id = parts[1]
        if not _TENANT_RE.fullmatch(tenant_id):
            raise ValidationError(detail=f"storage key names invalid tenant {tenant_id!r}")
        # The slug charset admits no separators, dots-only components, or
        # empties, so this one check covers traversal in every component.
        if not all(_SLUG_RE.fullmatch(part) for part in parts[2:]):
            raise ValidationError(detail="storage key contains unsafe components")
        return cls(tenant_id=tenant_id, path=parts)

    def __str__(self) -> str:
        return "/".join(self.path)


class ObjectStore(Protocol):
    """Where uploaded bytes live, keyed by server-derived :class:`StorageKey`.

    Implementations must refuse keys outside the tenant's prefix — the disk
    adapter enforces this with containment; the in-memory fake by construction.
    """

    async def put(self, key: StorageKey, content: bytes) -> None:
        """Write ``content`` under ``key``, replacing any previous blob.

        Raises:
            ValidationError: the key is not tenant-scoped.
        """
        ...

    async def read(self, key: StorageKey) -> bytes:
        """The blob stored under ``key``.

        Raises:
            NotFoundError: no blob exists under the key.
        """
        ...

    async def delete(self, key: StorageKey) -> None:
        """Remove the blob under ``key``, if it exists (idempotent)."""
        ...


class MemoryObjectStore:
    """Hermetic fake: tenant isolation by construction, bytes by key."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    async def put(self, key: StorageKey, content: bytes) -> None:
        self._blobs[str(key)] = content

    async def read(self, key: StorageKey) -> bytes:
        try:
            return self._blobs[str(key)]
        except KeyError as exc:
            raise NotFoundError(detail=f"object {key} is not stored") from exc

    async def delete(self, key: StorageKey) -> None:
        self._blobs.pop(str(key), None)


class DiskObjectStore:
    """Filesystem object storage for local development and single-node demo.

    Keys map to paths under ``root / key.path``. Containment is enforced twice:
    :meth:`_resolve` re-checks that the parsed key is tenant-scoped, and that
    the resolved path is inside the tenant's directory, so a hand-crafted key
    cannot escape even if a caller bypassed :meth:`StorageKey.parse`.

    Not a production multi-node object store; the port keeps that swap a
    contained change (`DEP-005`).
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def _resolve(self, key: StorageKey) -> Path:
        candidate = self._root.joinpath(*key.path)
        tenant_root = (self._root / "tenants" / key.tenant_id).resolve()
        resolved = candidate.resolve()
        if not (resolved == tenant_root or tenant_root in resolved.parents):
            raise ValidationError(detail="object key escapes the tenant's directory")
        return resolved

    async def put(self, key: StorageKey, content: bytes) -> None:
        target = self._resolve(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    async def read(self, key: StorageKey) -> bytes:
        target = self._resolve(key)
        if not target.is_file():
            raise NotFoundError(detail=f"object {key} is not stored")
        return target.read_bytes()

    async def delete(self, key: StorageKey) -> None:
        target = self._resolve(key)
        if target.is_file():
            target.unlink()
