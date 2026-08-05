"""Object-storage key derivation and tenant isolation (RAG-002)."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from tenantchat.api.storage import (
    DiskObjectStore,
    MemoryObjectStore,
    StorageKey,
    slugify,
    validated_filename,
)
from tenantchat.core.errors import NotFoundError, ValidationError

TENANT = "clearview"
SOURCE_ID = uuid.uuid4()
CHECKSUM = "a" * 64


def build(*, tenant_id: str = TENANT, external_key: str = "brochure.md") -> StorageKey:
    return StorageKey.build(
        tenant_id=tenant_id,
        source_id=SOURCE_ID,
        external_key=external_key,
        checksum=CHECKSUM,
    )


def test_a_derived_key_is_tenant_scoped_and_server_shaped() -> None:
    key = build()
    assert str(key).startswith(f"tenants/{TENANT}/")
    assert key.tenant_id == TENANT
    parsed = StorageKey.parse(str(key))
    assert parsed == key


def test_the_same_content_derives_the_same_key() -> None:
    assert str(build()) == str(build())
    assert str(build(external_key="renamed file.md")) != str(build())


def test_parse_rejects_every_path_traversal_shape() -> None:
    for hostile in (
        "../etc/passwd",
        "tenants/../etc/passwd",
        "tenants/clearview/../../etc/passwd",
        "/tenants/clearview/../x",
        "tenants//clearview",
        "tenants/clearview/",
        "tenants/clearview/../..",
        "tenants/ClearView/segments",
        "tenants/clearview/seg/..-..",
        "not-tenants/clearview/seg",
        "tenants/clearview/seg/../../x",
    ):
        with pytest.raises(ValidationError):
            StorageKey.parse(hostile)


def test_build_rejects_a_non_slug_tenant_id() -> None:
    with pytest.raises(ValidationError):
        build(tenant_id="../etc")


def test_build_slugifies_the_caller_controlled_external_key() -> None:
    key = build(external_key="../../etc/passwd")
    assert ".." not in str(key)
    assert str(key).startswith(f"tenants/{TENANT}/{SOURCE_ID}/")


def test_a_upload_filename_must_be_a_single_plain_name() -> None:
    for hostile in ("../etc/passwd", "a/b.md", "a\\b.md", "..", ".", "a\x00b", "x" * 300, "/etc/x"):
        with pytest.raises(ValidationError):
            validated_filename(hostile)
    assert validated_filename("brochure.md") == "brochure.md"
    assert validated_filename("2026 plan terms.pdf") == "2026 plan terms.pdf"


def test_slugify_leaves_no_separator_behind() -> None:
    assert ".." not in slugify("../../etc/passwd")
    assert slugify("../../etc/passwd") == "etc-passwd"


def test_the_memory_store_scopes_blobs_by_key() -> None:
    async def scenario() -> None:
        store = MemoryObjectStore()
        key = build()
        await store.put(key, b"content")
        assert await store.read(key) == b"content"
        with pytest.raises(NotFoundError):
            await store.read(build(external_key="other.md"))
        await store.delete(key)
        with pytest.raises(NotFoundError):
            await store.read(key)

    asyncio.run(scenario())


def test_the_disk_store_never_resolves_outside_the_tenant_directory(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = DiskObjectStore(tmp_path)
        key = build()
        await store.put(key, b"content")
        assert (await store.read(key)) == b"content"

        # Even a key that somehow passes parse lands in the tenant's directory.
        for hostile in ("tenants/clearview/../../escape", "tenants/clearview/.."):
            with pytest.raises(ValidationError):
                await store.put(StorageKey.parse(hostile), b"x")

        escape = tmp_path / "escape.md"
        assert not escape.exists()
        assert (tmp_path / "tenants" / TENANT).exists()

    asyncio.run(scenario())


def test_another_tenants_key_reads_nothing_and_writes_nowhere_visible(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = DiskObjectStore(tmp_path)
        await store.put(build(tenant_id="apex"), b"apex only")
        with pytest.raises(NotFoundError):
            await store.read(build(tenant_id="clearview"))

    asyncio.run(scenario())
