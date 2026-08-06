"""The `FEAT-008` review stores against real PostgreSQL: the feedback upsert,
the queue's closed status machine, the diagnosis overlay, and the immutable
eval-closure reference.

The queue's guarantees are durable here: enqueueing is idempotent per turn,
every transition is a guarded UPDATE that distinguishes "absent" from
"wrong state", a resubmission replaces the reviewer's overlay without touching
the turn's content, and the first evaluation pass wins and survives re-application.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from tenantchat.api.persistence import (
    Database,
    DatabasePoolSettings,
    PostgresReviewQueueStore,
    PostgresTurnFeedbackStore,
    PostgresTurnRecordStore,
)
from tenantchat.api.store import ReviewDiagnosis
from tenantchat.core.errors import NotFoundError, ReviewTransitionError

POOL = DatabasePoolSettings(size=2, max_overflow=1, timeout_seconds=5)

pytestmark = pytest.mark.integration

TENANT = "tenant-reviews"
OTHER_TENANT = "tenant-reviews-other"


@pytest.fixture
def database(repository_database_url: str) -> Iterator[Database]:
    db = Database.connect(repository_database_url, POOL)
    try:
        yield db
    finally:
        asyncio.run(db.dispose())


def _libpq(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)


def _seed_tenant(database_url: str, tenant_id: str) -> None:
    with psycopg.connect(_libpq(database_url)) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO tenants (id, display_name) VALUES (%s, %s) " "ON CONFLICT (id) DO NOTHING",
            (tenant_id, f"Tenant {tenant_id}"),
        )


def _seed_turn(database_url: str, tenant_id: str, session_id: uuid.UUID) -> uuid.UUID:
    _seed_tenant(database_url, tenant_id)
    with psycopg.connect(_libpq(database_url)) as connection:
        connection.execute(
            "INSERT INTO chat_sessions (id, tenant_id) VALUES (%s, %s)",
            (session_id, tenant_id),
        )
    database = Database.connect(database_url, POOL)
    try:
        turn = asyncio.run(
            PostgresTurnRecordStore(database.engine).record(
                tenant_id,
                session_id,
                content={
                    "diagnoses": [
                        {"cause": "provider_failure", "status": "confirmed"},
                        {"cause": "tool_error", "status": "detected"},
                    ],
                    "retrieval": {"query": "What are your hours?"},
                    "output": {"claims": []},
                },
                component_manifest_hash="a" * 64,
                diagnosis_causes=("provider_failure", "tool_error"),
                diagnosis_statuses=("confirmed", "detected"),
            )
        )
    finally:
        asyncio.run(database.dispose())
    return turn.turn_id


def _diagnosis_row(
    review_id: uuid.UUID,
    *,
    relationship: str = "confirms",
    automatic_index: int | None = 0,
    cause: str = "provider_failure",
    status: str = "confirmed",
) -> ReviewDiagnosis:
    return ReviewDiagnosis(
        diagnosis_id=uuid.uuid4(),
        tenant_id=TENANT,
        review_id=review_id,
        relationship=relationship,
        automatic_index=automatic_index,
        cause=cause,
        stage="model",
        role="primary",
        status=status,
        confidence="high",
        evidence=(),
        note=None,
        created_at=datetime.now(UTC),
    )


def test_enqueue_is_idempotent_per_turn(database: Database, repository_database_url: str) -> None:
    session_id = uuid.uuid4()
    turn_id = _seed_turn(repository_database_url, TENANT, session_id)
    store = PostgresReviewQueueStore(database.engine)

    first = asyncio.run(
        store.enqueue(
            TENANT,
            turn_id,
            source="automatic",
            priority=32,
            recurrence=1,
            manifest_hash="a" * 64,
            committed_actions=False,
            novel_manifest=True,
        )
    )
    second = asyncio.run(
        store.enqueue(
            TENANT,
            turn_id,
            source="user_feedback",
            priority=10,
            recurrence=1,
            manifest_hash="a" * 64,
            committed_actions=False,
            novel_manifest=False,
        )
    )
    assert second.review_id == first.review_id
    assert second.source == "automatic"  # the first source wins

    cases = asyncio.run(store.search(TENANT, limit=10))
    assert [case.review_id for case in cases] == [first.review_id]


def test_count_for_manifest_counts_prior_cases(
    database: Database, repository_database_url: str
) -> None:
    session_id = uuid.uuid4()
    first_turn = _seed_turn(repository_database_url, TENANT, session_id)
    second_turn = _seed_turn(repository_database_url, TENANT, uuid.uuid4())
    store = PostgresReviewQueueStore(database.engine)
    asyncio.run(
        store.enqueue(
            TENANT,
            first_turn,
            source="automatic",
            priority=32,
            recurrence=1,
            manifest_hash="a" * 64,
            committed_actions=False,
            novel_manifest=True,
        )
    )
    assert asyncio.run(store.count_for_manifest(TENANT, "a" * 64)) == 1
    asyncio.run(
        store.enqueue(
            TENANT,
            second_turn,
            source="automatic",
            priority=25,
            recurrence=2,
            manifest_hash="a" * 64,
            committed_actions=False,
            novel_manifest=False,
        )
    )
    assert asyncio.run(store.count_for_manifest(TENANT, "a" * 64)) == 2
    assert asyncio.run(store.count_for_manifest(OTHER_TENANT, "a" * 64)) == 0


def test_take_and_submit_follow_the_closed_status_machine(
    database: Database, repository_database_url: str
) -> None:
    session_id = uuid.uuid4()
    turn_id = _seed_turn(repository_database_url, TENANT, session_id)
    store = PostgresReviewQueueStore(database.engine)
    case = asyncio.run(
        store.enqueue(
            TENANT,
            turn_id,
            source="automatic",
            priority=32,
            recurrence=1,
            manifest_hash="a" * 64,
            committed_actions=False,
            novel_manifest=True,
        )
    )

    taken = asyncio.run(store.take(TENANT, case.review_id, reviewer="operator-7"))
    assert taken.status == "in_review"
    assert taken.reviewer_subject == "operator-7"

    # A second take on an in-review case is a transition conflict, not a 404.
    with pytest.raises(ReviewTransitionError):
        asyncio.run(store.take(TENANT, case.review_id, reviewer="operator-8"))

    submitted = asyncio.run(
        store.submit(
            TENANT,
            case.review_id,
            reviewer="operator-7",
            verdict="amended",
            note="The retry budget was exhausted",
            corrected_answer="We are open daily from 8 AM to 6 PM.",
            proposed_fix="Raise the retry budget",
            status="awaiting_fix",
            diagnoses=(
                _diagnosis_row(case.review_id, relationship="confirms", automatic_index=0),
                _diagnosis_row(
                    case.review_id,
                    relationship="amends",
                    automatic_index=1,
                    cause="tool_error",
                    status="confirmed",
                ),
            ),
        )
    )
    assert submitted.status == "awaiting_fix"
    assert submitted.verdict == "amended"
    assert submitted.corrected_answer == "We are open daily from 8 AM to 6 PM."

    rows = asyncio.run(store.diagnoses(TENANT, case.review_id))
    assert [row.relationship for row in rows] == ["confirms", "amends"]
    assert rows[1].automatic_index == 1

    # A resubmission replaces the overlay instead of stacking it.
    asyncio.run(
        store.submit(
            TENANT,
            case.review_id,
            reviewer="operator-7",
            verdict="confirmed",
            note=None,
            corrected_answer=None,
            proposed_fix=None,
            status="awaiting_fix",
            diagnoses=(
                _diagnosis_row(case.review_id, relationship="confirms", automatic_index=0),
                _diagnosis_row(case.review_id, relationship="confirms", automatic_index=1),
            ),
        )
    )
    rows = asyncio.run(store.diagnoses(TENANT, case.review_id))
    assert [row.relationship for row in rows] == ["confirms", "confirms"]

    with pytest.raises(ReviewTransitionError):
        asyncio.run(store.take(TENANT, case.review_id, reviewer="operator-8"))


def test_the_first_eval_pass_wins_and_survives_reapplication(
    database: Database, repository_database_url: str
) -> None:
    session_id = uuid.uuid4()
    turn_id = _seed_turn(repository_database_url, TENANT, session_id)
    store = PostgresReviewQueueStore(database.engine)
    case = asyncio.run(
        store.enqueue(
            TENANT,
            turn_id,
            source="automatic",
            priority=32,
            recurrence=1,
            manifest_hash="a" * 64,
            committed_actions=False,
            novel_manifest=True,
        )
    )
    asyncio.run(
        store.submit(
            TENANT,
            case.review_id,
            reviewer="operator-7",
            verdict="confirmed",
            note=None,
            corrected_answer=None,
            proposed_fix="Fix the provider client",
            status="awaiting_fix",
            diagnoses=(_diagnosis_row(case.review_id),),
        )
    )
    case = asyncio.run(
        store.set_case_id(TENANT, case.review_id, case_id=f"review-{case.review_id}")
    )

    moment = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    closed = asyncio.run(
        store.record_eval_pass(
            TENANT,
            case.review_id,
            run_id="run-1",
            case_id=case.case_id or "",
            passed_at=moment,
        )
    )
    assert closed.status == "resolved"
    assert closed.closing_eval_run_id == "run-1"
    assert closed.closing_eval_passed_at == moment

    # Re-applying the report — or a later, different one — cannot rewrite the
    # first passing run (acceptance 5).
    later = asyncio.run(
        store.record_eval_pass(
            TENANT,
            case.review_id,
            run_id="run-2",
            case_id=case.case_id or "",
            passed_at=moment + timedelta(hours=1),
        )
    )
    assert later.closing_eval_run_id == "run-1"
    assert later.closing_eval_passed_at == moment


def test_for_case_ids_finds_only_the_matching_tenant_and_case(
    database: Database, repository_database_url: str
) -> None:
    session_id = uuid.uuid4()
    turn_id = _seed_turn(repository_database_url, TENANT, session_id)
    other_session = uuid.uuid4()
    other_turn = _seed_turn(repository_database_url, OTHER_TENANT, other_session)
    store = PostgresReviewQueueStore(database.engine)
    case = asyncio.run(
        store.enqueue(
            TENANT,
            turn_id,
            source="automatic",
            priority=32,
            recurrence=1,
            manifest_hash="a" * 64,
            committed_actions=False,
            novel_manifest=True,
        )
    )
    other = asyncio.run(
        store.enqueue(
            OTHER_TENANT,
            other_turn,
            source="automatic",
            priority=32,
            recurrence=1,
            manifest_hash="b" * 64,
            committed_actions=False,
            novel_manifest=True,
        )
    )
    asyncio.run(store.set_case_id(TENANT, case.review_id, case_id="case-one"))
    asyncio.run(store.set_case_id(OTHER_TENANT, other.review_id, case_id="case-two"))

    matches = asyncio.run(store.for_case_ids(TENANT, {"case-one", "case-two"}))
    assert [case.review_id for case in matches] == [case.review_id]
    assert asyncio.run(store.for_case_ids(TENANT, set())) == ()


def test_feedback_upsert_replaces_the_rating(
    database: Database, repository_database_url: str
) -> None:
    session_id = uuid.uuid4()
    turn_id = _seed_turn(repository_database_url, TENANT, session_id)
    store = PostgresTurnFeedbackStore(database.engine)

    first = asyncio.run(store.record(TENANT, turn_id, rating="down", reason="Wrong price"))
    second = asyncio.run(store.record(TENANT, turn_id, rating="up", reason=None))
    assert second.feedback_id == first.feedback_id
    assert second.rating == "up"
    assert second.reason is None
    fetched = asyncio.run(store.for_turn(TENANT, turn_id))
    assert fetched is not None and fetched.rating == "up"


def test_feedback_and_reviews_are_tenant_qualified(
    database: Database, repository_database_url: str
) -> None:
    session_id = uuid.uuid4()
    turn_id = _seed_turn(repository_database_url, TENANT, session_id)
    feedback = PostgresTurnFeedbackStore(database.engine)
    reviews = PostgresReviewQueueStore(database.engine)

    with pytest.raises(NotFoundError):
        asyncio.run(feedback.record(OTHER_TENANT, turn_id, rating="down", reason=None))
    with pytest.raises(NotFoundError):
        asyncio.run(
            reviews.enqueue(
                OTHER_TENANT,
                turn_id,
                source="automatic",
                priority=1,
                recurrence=1,
                manifest_hash="a" * 64,
                committed_actions=False,
                novel_manifest=True,
            )
        )
    case = asyncio.run(
        reviews.enqueue(
            TENANT,
            turn_id,
            source="automatic",
            priority=1,
            recurrence=1,
            manifest_hash="a" * 64,
            committed_actions=False,
            novel_manifest=True,
        )
    )
    with pytest.raises(NotFoundError):
        asyncio.run(reviews.get(OTHER_TENANT, case.review_id))
    with pytest.raises(NotFoundError):
        asyncio.run(reviews.take(OTHER_TENANT, case.review_id, reviewer="operator-7"))
