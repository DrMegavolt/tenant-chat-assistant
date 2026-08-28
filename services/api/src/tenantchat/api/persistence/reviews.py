"""PostgreSQL adapters for the `FEAT-008` review surface.

``PostgresTurnFeedbackStore`` records visitor ratings idempotently, one row per
turn record. ``PostgresReviewQueueStore`` owns the closed status machine of the
queue: enqueueing is an idempotent upsert on ``(tenant_id, turn_record_id)``,
every transition is a guarded ``UPDATE`` whose row count decides the conflict,
and the reviewer's diagnosis rows are replaced wholesale on resubmission —
never merged with the detector's records, which stay inside the turn's opaque
content object.

The evaluation-closure write (:meth:`PostgresReviewQueueStore.record_eval_pass`)
guards on the closing reference being unset, so re-applying a report cannot
move the case backward: the first passing run wins and the reference survives
for the life of the row.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tenantchat.api.persistence.repositories import _insert_audit_event
from tenantchat.api.persistence.tenancy import require_active_tenant
from tenantchat.api.store import AuditEvent, ReviewCase, ReviewDiagnosis, TurnFeedback
from tenantchat.core.errors import NotFoundError, ReviewTransitionError

_MAX_REVIEW_SEARCH_LIMIT = 200

_REVIEW_COLUMNS = (
    "id",
    "tenant_id",
    "turn_record_id",
    "source",
    "status",
    "priority",
    "recurrence",
    "manifest_hash",
    "committed_actions",
    "novel_manifest",
    "case_id",
    "reviewer_subject",
    "reviewed_at",
    "verdict",
    "verdict_note",
    "corrected_answer",
    "proposed_fix",
    "closing_eval_run_id",
    "closing_eval_case_id",
    "closing_eval_passed_at",
    "created_at",
    "updated_at",
)

# A module constant, never a format site: every mutation returns the same
# columns, and the statement is assembled at import time exactly like
# `_REVIEW_SELECT`.
_REVIEW_SELECT = "SELECT " + ", ".join(_REVIEW_COLUMNS)
_REVIEW_RETURNING = "RETURNING " + ", ".join(_REVIEW_COLUMNS)
_TAKE_SQL = (
    "UPDATE review_queue "
    "SET status = 'in_review', reviewer_subject = :reviewer, updated_at = now() "
    "WHERE tenant_id = :tenant_id AND id = :review_id AND status = 'open' " + _REVIEW_RETURNING
)
_SUBMIT_SQL = (
    "UPDATE review_queue "
    "SET status = :status, reviewer_subject = :reviewer, "
    "reviewed_at = now(), verdict = :verdict, verdict_note = :note, "
    "corrected_answer = :corrected, proposed_fix = :fix, updated_at = now() "
    "WHERE tenant_id = :tenant_id AND id = :review_id "
    "AND status IN ('open', 'in_review', 'awaiting_fix') " + _REVIEW_RETURNING
)
_EVAL_PASS_SQL = (
    "UPDATE review_queue "
    "SET status = 'resolved', "
    "closing_eval_run_id = :run_id, closing_eval_case_id = :case_id, "
    "closing_eval_passed_at = :passed_at, updated_at = :passed_at "
    "WHERE tenant_id = :tenant_id AND id = :review_id "
    "AND status = 'awaiting_fix' AND closing_eval_run_id IS NULL " + _REVIEW_RETURNING
)
_SET_CASE_ID_SQL = (
    "UPDATE review_queue "
    "SET case_id = :case_id, updated_at = now() "
    "WHERE tenant_id = :tenant_id AND id = :review_id " + _REVIEW_RETURNING
)


_DIAGNOSIS_COLUMNS = (
    "id",
    "tenant_id",
    "review_id",
    "relationship",
    "automatic_index",
    "cause",
    "stage",
    "role",
    "status",
    "confidence",
    "evidence",
    "note",
    "created_at",
)

_DIAGNOSIS_SELECT = "SELECT " + ", ".join(_DIAGNOSIS_COLUMNS)


def _review(row: object) -> ReviewCase:
    mapping = row._mapping  # type: ignore[attr-defined]
    return ReviewCase(
        review_id=mapping["id"],
        tenant_id=mapping["tenant_id"],
        turn_id=mapping["turn_record_id"],
        source=mapping["source"],
        status=mapping["status"],
        priority=mapping["priority"],
        recurrence=mapping["recurrence"],
        manifest_hash=mapping["manifest_hash"],
        committed_actions=mapping["committed_actions"],
        novel_manifest=mapping["novel_manifest"],
        case_id=mapping["case_id"],
        reviewer_subject=mapping["reviewer_subject"],
        reviewed_at=mapping["reviewed_at"],
        verdict=mapping["verdict"],
        verdict_note=mapping["verdict_note"],
        corrected_answer=mapping["corrected_answer"],
        proposed_fix=mapping["proposed_fix"],
        closing_eval_run_id=mapping["closing_eval_run_id"],
        closing_eval_case_id=mapping["closing_eval_case_id"],
        closing_eval_passed_at=mapping["closing_eval_passed_at"],
        created_at=mapping["created_at"],
        updated_at=mapping["updated_at"],
    )


def _diagnosis(row: object) -> ReviewDiagnosis:
    mapping = row._mapping  # type: ignore[attr-defined]
    return ReviewDiagnosis(
        diagnosis_id=mapping["id"],
        tenant_id=mapping["tenant_id"],
        review_id=mapping["review_id"],
        relationship=mapping["relationship"],
        automatic_index=mapping["automatic_index"],
        cause=mapping["cause"],
        stage=mapping["stage"],
        role=mapping["role"],
        status=mapping["status"],
        confidence=mapping["confidence"],
        evidence=tuple(mapping["evidence"]),
        note=mapping["note"],
        created_at=mapping["created_at"],
    )


def _conflict(review_id: uuid.UUID, current: str, *, allowed: str) -> ReviewTransitionError:
    return ReviewTransitionError(
        current=current,
        permitted=frozenset({allowed}),
        detail=f"review {review_id} is {current}, not {allowed}",
    )


class PostgresTurnFeedbackStore:
    """Visitor ratings, idempotently upserted per turn record."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def record(
        self,
        tenant_id: str,
        turn_id: uuid.UUID,
        *,
        rating: str,
        reason: str | None,
    ) -> TurnFeedback:
        try:
            async with self._engine.begin() as connection:
                # The composite foreign key is the proof the turn exists and
                # belongs to this tenant; its violation is indistinguishable
                # from an absent turn, so nothing about another conversation
                # can be learned through this endpoint.
                await require_active_tenant(connection, tenant_id)
                result = await connection.execute(
                    text(
                        """
                        INSERT INTO turn_feedback
                            (id, tenant_id, turn_record_id, rating, reason)
                        VALUES
                            (:id, :tenant_id, :turn_id, :rating, :reason)
                        ON CONFLICT (tenant_id, turn_record_id)
                        DO UPDATE SET rating = EXCLUDED.rating, reason = EXCLUDED.reason
                        RETURNING id, tenant_id, turn_record_id, rating, reason, created_at
                        """
                    ),
                    {
                        "id": uuid.uuid4(),
                        "tenant_id": tenant_id,
                        "turn_id": turn_id,
                        "rating": rating,
                        "reason": reason,
                    },
                )
                row = result.one()
        except IntegrityError as exc:
            raise NotFoundError(detail="turn record absent or outside tenant") from exc
        return TurnFeedback(
            feedback_id=row.id,
            tenant_id=row.tenant_id,
            turn_id=row.turn_record_id,
            rating=row.rating,
            reason=row.reason,
            created_at=row.created_at,
        )

    async def for_turn(self, tenant_id: str, turn_id: uuid.UUID) -> TurnFeedback | None:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT id, tenant_id, turn_record_id, rating, reason, created_at
                    FROM turn_feedback
                    WHERE tenant_id = :tenant_id AND turn_record_id = :turn_id
                    """
                ),
                {"tenant_id": tenant_id, "turn_id": turn_id},
            )
            row = result.first()
        if row is None:
            return None
        return TurnFeedback(
            feedback_id=row.id,
            tenant_id=row.tenant_id,
            turn_id=row.turn_record_id,
            rating=row.rating,
            reason=row.reason,
            created_at=row.created_at,
        )


class PostgresReviewQueueStore:
    """The queue, its diagnosis overlay, and the eval-closure reference."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def enqueue(
        self,
        tenant_id: str,
        turn_id: uuid.UUID,
        *,
        source: str,
        priority: int,
        recurrence: int,
        manifest_hash: str,
        committed_actions: bool,
        novel_manifest: bool,
    ) -> ReviewCase:
        try:
            async with self._engine.begin() as connection:
                await require_active_tenant(connection, tenant_id)
                result = await connection.execute(
                    text(
                        f"""
                        INSERT INTO review_queue
                            (id, tenant_id, turn_record_id, source, priority, recurrence,
                             manifest_hash, committed_actions, novel_manifest)
                        VALUES
                            (:id, :tenant_id, :turn_id, :source, :priority, :recurrence,
                             :manifest_hash, :committed_actions, :novel_manifest)
                        ON CONFLICT (tenant_id, turn_record_id)
                        DO NOTHING
                        RETURNING {", ".join(_REVIEW_COLUMNS)}
                        """
                    ),
                    {
                        "id": uuid.uuid4(),
                        "tenant_id": tenant_id,
                        "turn_id": turn_id,
                        "source": source,
                        "priority": priority,
                        "recurrence": recurrence,
                        "manifest_hash": manifest_hash,
                        "committed_actions": committed_actions,
                        "novel_manifest": novel_manifest,
                    },
                )
                row = result.first()
                if row is None:
                    return _review(
                        (
                            await connection.execute(
                                text(
                                    _REVIEW_SELECT + " FROM review_queue "
                                    "WHERE tenant_id = :tenant_id AND turn_record_id = :turn_id"
                                ),
                                {"tenant_id": tenant_id, "turn_id": turn_id},
                            )
                        ).one()
                    )
                return _review(row)
        except IntegrityError as exc:
            raise NotFoundError(detail="turn record absent or outside tenant") from exc

    async def get(self, tenant_id: str, review_id: uuid.UUID) -> ReviewCase:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    _REVIEW_SELECT
                    + " FROM review_queue WHERE tenant_id = :tenant_id AND id = :review_id"
                ),
                {"tenant_id": tenant_id, "review_id": review_id},
            )
            row = result.first()
        if row is None:
            raise NotFoundError(detail="review case absent or outside tenant")
        return _review(row)

    async def for_turn(self, tenant_id: str, turn_id: uuid.UUID) -> ReviewCase | None:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    _REVIEW_SELECT + " FROM review_queue "
                    "WHERE tenant_id = :tenant_id AND turn_record_id = :turn_id"
                ),
                {"tenant_id": tenant_id, "turn_id": turn_id},
            )
            row = result.first()
        return None if row is None else _review(row)

    async def search(
        self,
        tenant_id: str,
        *,
        statuses: tuple[str, ...] = (),
        limit: int = 50,
    ) -> tuple[ReviewCase, ...]:
        clauses = ["tenant_id = :tenant_id"]
        params: dict[str, object] = {"tenant_id": tenant_id}
        if statuses:
            clauses.append("status = ANY(:statuses)")
            params["statuses"] = list(statuses)
        bounded = min(limit, _MAX_REVIEW_SEARCH_LIMIT)
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    _REVIEW_SELECT
                    + " FROM review_queue WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY priority DESC, created_at, id LIMIT :limit"
                ),
                {**params, "limit": bounded},
            )
            return tuple(_review(row) for row in result.all())

    async def take(
        self,
        tenant_id: str,
        review_id: uuid.UUID,
        *,
        reviewer: str,
        audit_event: AuditEvent | None = None,
    ) -> ReviewCase:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(_TAKE_SQL),
                {"tenant_id": tenant_id, "review_id": review_id, "reviewer": reviewer},
            )
            row = result.first()
            if row is None:
                await self._raise_transition(connection, tenant_id, review_id, allowed="open")
            if audit_event is not None:
                # The decision and its accountability row commit together (R-39).
                await _insert_audit_event(connection, audit_event)
            return _review(row)

    async def count_for_manifest(self, tenant_id: str, manifest_hash: str) -> int:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    "SELECT count(*) FROM review_queue "
                    "WHERE tenant_id = :tenant_id AND manifest_hash = :manifest_hash"
                ),
                {"tenant_id": tenant_id, "manifest_hash": manifest_hash},
            )
            return int(result.scalar_one())

    async def submit(
        self,
        tenant_id: str,
        review_id: uuid.UUID,
        *,
        reviewer: str,
        verdict: str,
        note: str | None,
        corrected_answer: str | None,
        proposed_fix: str | None,
        status: str,
        diagnoses: tuple[ReviewDiagnosis, ...],
        audit_event: AuditEvent | None = None,
    ) -> ReviewCase:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(_SUBMIT_SQL),
                {
                    "tenant_id": tenant_id,
                    "review_id": review_id,
                    "reviewer": reviewer,
                    "verdict": verdict,
                    "note": note,
                    "corrected": corrected_answer,
                    "fix": proposed_fix,
                    "status": status,
                },
            )
            row = result.first()
            if row is None:
                await self._raise_transition(
                    connection, tenant_id, review_id, allowed="open, in_review, or awaiting_fix"
                )
            await connection.execute(
                text(
                    "DELETE FROM review_diagnoses "
                    "WHERE tenant_id = :tenant_id AND review_id = :review_id"
                ),
                {"tenant_id": tenant_id, "review_id": review_id},
            )
            for diagnosis in diagnoses:
                await connection.execute(
                    text(
                        """
                        INSERT INTO review_diagnoses
                            (id, tenant_id, review_id, relationship, automatic_index,
                             cause, stage, role, status, confidence, evidence, note,
                             created_at)
                        VALUES
                            (:id, :tenant_id, :review_id, :relationship, :automatic_index,
                             :cause, :stage, :role, :status, :confidence, :evidence, :note,
                             :created_at)
                        """
                    ),
                    {
                        "id": uuid.uuid4(),
                        "tenant_id": tenant_id,
                        "review_id": review_id,
                        "relationship": diagnosis.relationship,
                        "automatic_index": diagnosis.automatic_index,
                        "cause": diagnosis.cause,
                        "stage": diagnosis.stage,
                        "role": diagnosis.role,
                        "status": diagnosis.status,
                        "confidence": diagnosis.confidence,
                        "evidence": list(diagnosis.evidence),
                        "note": diagnosis.note,
                        # Per-row timestamps from Python, not the transaction
                        # time: PostgreSQL's now() is the same for every row in
                        # one transaction, which would make the ORDER BY a coin
                        # toss and the overlay order nondeterministic.
                        "created_at": datetime.now(UTC),
                    },
                )
            if audit_event is not None:
                await _insert_audit_event(connection, audit_event)
            return _review(row)

    async def record_eval_pass(
        self,
        tenant_id: str,
        review_id: uuid.UUID,
        *,
        run_id: str,
        case_id: str,
        passed_at: datetime,
    ) -> ReviewCase:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(_EVAL_PASS_SQL),
                {
                    "tenant_id": tenant_id,
                    "review_id": review_id,
                    "run_id": run_id,
                    "case_id": case_id,
                    "passed_at": passed_at,
                },
            )
            row = result.first()
            if row is None:
                result = await connection.execute(
                    text(
                        _REVIEW_SELECT + " FROM review_queue "
                        "WHERE tenant_id = :tenant_id AND id = :review_id"
                    ),
                    {"tenant_id": tenant_id, "review_id": review_id},
                )
                existing = result.first()
                if existing is None:
                    raise NotFoundError(detail="review case absent or outside tenant")
                return _review(existing)
            return _review(row)

    async def set_case_id(
        self, tenant_id: str, review_id: uuid.UUID, *, case_id: str
    ) -> ReviewCase:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(_SET_CASE_ID_SQL),
                {"tenant_id": tenant_id, "review_id": review_id, "case_id": case_id},
            )
            row = result.first()
            if row is None:
                raise NotFoundError(detail="review case absent or outside tenant")
            return _review(row)

    async def diagnoses(self, tenant_id: str, review_id: uuid.UUID) -> tuple[ReviewDiagnosis, ...]:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    _DIAGNOSIS_SELECT + " FROM review_diagnoses "
                    "WHERE tenant_id = :tenant_id AND review_id = :review_id "
                    "ORDER BY created_at, id"
                ),
                {"tenant_id": tenant_id, "review_id": review_id},
            )
            return tuple(_diagnosis(row) for row in result.all())

    async def for_case_ids(
        self, tenant_id: str, case_ids: Collection[str]
    ) -> tuple[ReviewCase, ...]:
        if not case_ids:
            return ()
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    _REVIEW_SELECT + " FROM review_queue "
                    "WHERE tenant_id = :tenant_id AND case_id = ANY(:case_ids)"
                ),
                {"tenant_id": tenant_id, "case_ids": list(case_ids)},
            )
            return tuple(_review(row) for row in result.all())

    async def _raise_transition(
        self,
        connection: AsyncConnection,
        tenant_id: str,
        review_id: uuid.UUID,
        *,
        allowed: str,
    ) -> ReviewCase:
        """The guarded UPDATE matched nothing; distinguish absent from wrong-state.

        Runs inside the caller's transaction, reusing its connection so the
        read that decides the error sees the same snapshot as the failed write.
        """
        result = await connection.execute(
            text(
                _REVIEW_SELECT + " FROM review_queue "
                "WHERE tenant_id = :tenant_id AND id = :review_id"
            ),
            {"tenant_id": tenant_id, "review_id": review_id},
        )
        row = result.first()
        if row is None:
            raise NotFoundError(detail="review case absent or outside tenant")
        raise _conflict(review_id, row.status, allowed=allowed)
