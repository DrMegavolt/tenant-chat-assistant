"""R-10: an in-flight idempotency claim is a lease, not a life sentence.

A worker that crashes between claiming a key and completing it must not block
that key forever: the row's ``expires_at`` bounds the lock, the next claimant
takes the key over once the window closes, and the sweep drops rows past
retention so the table stays a working set. The in-memory store shares the
PostgreSQL store's refusal order and expiry clock, so these tests prove the
contract without a database.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from tenantchat.api.store import IDEMPOTENCY_RETENTION, InMemoryIdempotencyStore
from tenantchat.core.errors import ConflictError
from tenantchat.core.ports import IdempotencyKey

SCOPE = "booking_confirm"


class _Clock:
    """A controllable clock, so a test can cross the retention window."""

    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def _key(value: str = "key-1") -> IdempotencyKey:
    return IdempotencyKey(value=value)


def test_an_expired_in_flight_claim_stops_blocking_its_key() -> None:
    """A crashed attempt is reclaimable: the same key and fingerprint claim
    again once the claim's window closes, instead of conflicting forever."""
    clock = _Clock()
    store = InMemoryIdempotencyStore(now=clock)

    first = asyncio.run(store.begin("t1", scope=SCOPE, key=_key(), fingerprint="fp"))
    assert first is None

    clock.advance(IDEMPOTENCY_RETENTION + timedelta(seconds=1))
    second = asyncio.run(store.begin("t1", scope=SCOPE, key=_key(), fingerprint="fp"))
    assert second is None

    asyncio.run(store.complete("t1", scope=SCOPE, key=_key(), response={"booking_id": "BK-1"}))
    answer = asyncio.run(store.begin("t1", scope=SCOPE, key=_key(), fingerprint="fp"))
    assert answer == {"booking_id": "BK-1"}


def test_a_live_in_flight_claim_still_conflicts() -> None:
    """Recovery kicks in only past retention: a concurrent attempt — another
    worker mid-flight on the same key — is still told the attempt is running."""
    clock = _Clock()
    store = InMemoryIdempotencyStore(now=clock)
    asyncio.run(store.begin("t1", scope=SCOPE, key=_key(), fingerprint="fp"))

    clock.advance(IDEMPOTENCY_RETENTION - timedelta(hours=1))
    with pytest.raises(ConflictError) as conflict:
        asyncio.run(store.begin("t1", scope=SCOPE, key=_key(), fingerprint="fp"))
    assert "still in flight" in (conflict.value.detail or "")


def test_an_expired_claim_with_a_different_fingerprint_is_still_a_conflict() -> None:
    """Expiry recovers a crashed attempt, it does not launder key reuse: the
    same key naming a different request is refused even past retention."""
    clock = _Clock()
    store = InMemoryIdempotencyStore(now=clock)
    asyncio.run(store.begin("t1", scope=SCOPE, key=_key(), fingerprint="fp"))

    clock.advance(IDEMPOTENCY_RETENTION + timedelta(seconds=1))
    with pytest.raises(ConflictError) as conflict:
        asyncio.run(store.begin("t1", scope=SCOPE, key=_key(), fingerprint="other"))
    assert "different booking_confirm" in (conflict.value.detail or "")


def test_a_completed_answer_serves_retries_until_it_is_swept() -> None:
    """A retry arriving after the answer's window has technically closed still
    gets the stored answer, exactly as the PostgreSQL store does while the row
    waits for the sweep — reclaiming would re-execute an effect that committed."""
    clock = _Clock()
    store = InMemoryIdempotencyStore(now=clock)
    asyncio.run(store.begin("t1", scope=SCOPE, key=_key(), fingerprint="fp"))
    asyncio.run(store.complete("t1", scope=SCOPE, key=_key(), response={"booking_id": "BK-1"}))

    clock.advance(IDEMPOTENCY_RETENTION + timedelta(seconds=1))
    answer = asyncio.run(store.begin("t1", scope=SCOPE, key=_key(), fingerprint="fp"))
    assert answer == {"booking_id": "BK-1"}


def test_the_sweep_drops_only_rows_past_retention() -> None:
    """The sweep is what keeps the table a working set: finished answers and
    crashed claims inside their windows survive untouched."""
    clock = _Clock()
    store = InMemoryIdempotencyStore(now=clock)
    asyncio.run(store.begin("t1", scope=SCOPE, key=_key("expired-inflight"), fingerprint="fp"))
    asyncio.run(store.begin("t1", scope=SCOPE, key=_key("expired-done"), fingerprint="fp"))
    asyncio.run(store.complete("t1", scope=SCOPE, key=_key("expired-done"), response={"ok": True}))
    # The live pair claims a fresh window, so it outlives the sweep below.
    clock.advance(timedelta(days=6))
    asyncio.run(store.begin("t1", scope=SCOPE, key=_key("live-inflight"), fingerprint="fp"))
    asyncio.run(store.begin("t1", scope=SCOPE, key=_key("live-done"), fingerprint="fp"))
    asyncio.run(store.complete("t1", scope=SCOPE, key=_key("live-done"), response={"ok": True}))
    clock.advance(timedelta(days=1, seconds=1))
    removed = asyncio.run(store.sweep())

    assert removed == 2
    swept_answer_key = asyncio.run(
        store.begin("t1", scope=SCOPE, key=_key("expired-done"), fingerprint="fp")
    )
    assert swept_answer_key is None
    still_live = asyncio.run(
        store.begin("t1", scope=SCOPE, key=_key("live-done"), fingerprint="fp")
    )
    assert still_live == {"ok": True}


def test_after_the_sweep_a_swept_key_claims_fresh() -> None:
    """A key whose answer was swept is free again — an effect re-attempted
    after the retention window re-runs, which is the documented trade: the
    table is a working set, not an archive."""
    clock = _Clock()
    store = InMemoryIdempotencyStore(now=clock)
    asyncio.run(store.begin("t1", scope=SCOPE, key=_key(), fingerprint="fp"))
    asyncio.run(store.complete("t1", scope=SCOPE, key=_key(), response={"ok": True}))
    clock.advance(IDEMPOTENCY_RETENTION + timedelta(seconds=1))
    assert asyncio.run(store.sweep()) == 1

    claimed = asyncio.run(store.begin("t1", scope=SCOPE, key=_key(), fingerprint="fp"))
    assert claimed is None
