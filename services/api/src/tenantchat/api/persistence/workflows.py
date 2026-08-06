"""The durable routing and workflow records over PostgreSQL (`AGENT-001`).

This is a system-of-record adapter: the tables here are what survives a wiped
checkpoint store, which is what the `ADR-0001` invariant demands of every
business record. The routing decision is persisted whole — every candidate, the
chosen intent, the confidence, the policy version, and the thresholds — and the
workflow row carries the collected fields, the pending confirmation, the tool
results, and the next allowed actions.

Replay safety comes from two constraints rather than a read-then-write:

- ``routing_decisions`` is unique on ``(chat_session_id, turn_index)``, so a
  replayed route node rewrites its own row.
- ``workflow_events`` is unique on ``(workflow_id, key_hash)``, where the hash
  is the digest of the caller's idempotency key; a replayed transition finds
  its own event and returns the current row without re-applying.

The workflow session is resolved to a real ``chat_sessions`` row the same way
the other action stores do it, so a runtime-level caller (which names a session
the API has not opened) still lands on a row the foreign keys can reference.
Reads are deliberately permissive: the router asks "is there a current
workflow?" before anything has been written, and the answer for a brand-new
session is ``None``, not an error.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tenantchat.api.persistence.repositories import _action_session
from tenantchat.api.store import (
    RoutingRow,
    WorkflowEventRow,
    WorkflowRow,
    _row_state,
    _state_row,
)
from tenantchat.core.errors import NotFoundError
from tenantchat.core.ports import IdempotencyKey
from tenantchat.core.routing import (
    IntentCandidate,
    IntentName,
    RoutingDecision,
    RoutingOutcome,
    RoutingRule,
)
from tenantchat.core.workflows import (
    ToolResult,
    WorkflowStatus,
    WorkflowTransition,
    transition_workflow,
)

# The one column list every SELECT shares, so a schema change is one edit.
_WORKFLOW_COLUMNS = """
    id, tenant_id, chat_session_id, intent, agent_version, status,
    collected_fields, pending_confirmation, tool_results, next_allowed_actions,
    turn_index, created_at, updated_at, completed_at
"""

_ROUTING_COLUMNS = """
    id, tenant_id, chat_session_id, turn_index,
    policy_version, agent_version, outcome, rule,
    chosen_intent, confidence, candidates,
    direct_threshold, clarify_threshold, conflict_gap, created_at
"""


def _hashed(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _candidates(rows: object) -> tuple[IntentCandidate, ...]:
    assert isinstance(rows, list)  # noqa: S101 - a database row we wrote
    candidates: list[IntentCandidate] = []
    for item in rows:
        assert isinstance(item, dict)  # noqa: S101 - a database row we wrote
        candidates.append(
            IntentCandidate(
                intent=IntentName(str(item["intent"])),
                score=float(item["score"]),
                matched_signals=tuple(str(signal) for signal in item.get("matched_signals", ())),
            )
        )
    return tuple(candidates)


def _routing_row(row: object) -> RoutingRow:
    mapping = row._mapping  # type: ignore[attr-defined]
    return RoutingRow(
        turn_index=mapping["turn_index"],
        tenant_id=mapping["tenant_id"],
        session_id=str(mapping["chat_session_id"]),
        policy_version=mapping["policy_version"],
        agent_version=mapping["agent_version"],
        outcome=RoutingOutcome(mapping["outcome"]),
        rule=RoutingRule(mapping["rule"]),
        chosen_intent=(
            IntentName(mapping["chosen_intent"]) if mapping["chosen_intent"] is not None else None
        ),
        confidence=mapping["confidence"],
        candidates=_candidates(mapping["candidates"]),
        direct_threshold=mapping["direct_threshold"],
        clarify_threshold=mapping["clarify_threshold"],
        conflict_gap=mapping["conflict_gap"],
        created_at=mapping["created_at"],
    )


def _workflow_row(row: object) -> WorkflowRow:
    mapping = row._mapping  # type: ignore[attr-defined]
    results = mapping["tool_results"]
    assert isinstance(results, list)  # noqa: S101 - a database row we wrote
    actions = mapping["next_allowed_actions"]
    assert isinstance(actions, list)  # noqa: S101 - a database row we wrote
    fields = mapping["collected_fields"]
    assert isinstance(fields, dict)  # noqa: S101 - a database row we wrote
    pending = mapping["pending_confirmation"]
    return WorkflowRow(
        workflow_id=_wf_id(mapping["id"]),
        tenant_id=mapping["tenant_id"],
        session_id=str(mapping["chat_session_id"]),
        intent=IntentName(mapping["intent"]),
        agent_version=mapping["agent_version"],
        status=WorkflowStatus(mapping["status"]),
        collected_fields={str(key): str(value) for key, value in fields.items()},
        pending_confirmation=dict(pending) if pending is not None else None,
        tool_results=tuple(dict(item) for item in results),
        next_allowed_actions=tuple(str(item) for item in actions),
        turn_index=mapping["turn_index"],
        created_at=mapping["created_at"],
        updated_at=mapping["updated_at"],
        completed_at=mapping["completed_at"],
    )


def _event_row(row: object) -> WorkflowEventRow:
    mapping = row._mapping  # type: ignore[attr-defined]
    return WorkflowEventRow(
        workflow_id=_wf_id(mapping["workflow_id"]),
        kind=mapping["transition"],
        payload=dict(mapping["payload"]),
        created_at=mapping["created_at"],
    )


def _wf_id(uuid_id: uuid.UUID) -> str:
    return f"wf-{uuid_id.hex.upper()}"


def _wf_uuid(workflow_id: str) -> uuid.UUID:
    return uuid.UUID(workflow_id.removeprefix("wf-"))


class PostgresWorkflowStore:
    """The routing and workflow tables, tenant-qualified in every statement."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def record_routing(
        self,
        *,
        tenant_id: str,
        session_id: str,
        turn_index: int,
        decision: RoutingDecision,
        agent_version: str,
        idempotency_key: IdempotencyKey,
    ) -> None:
        del idempotency_key  # the (session, turn) unique is the replay guard
        async with self._engine.begin() as connection:
            authoritative = await _action_session(connection, tenant_id, session_id)
            await connection.execute(
                text(
                    f"""
                    INSERT INTO routing_decisions ({_ROUTING_COLUMNS})
                    VALUES (
                        :id, :tenant_id, :session_id, :turn_index,
                        :policy_version, :agent_version, :outcome, :rule,
                        :chosen_intent, :confidence, :candidates,
                        :direct_threshold, :clarify_threshold, :conflict_gap,
                        now()
                    )
                    ON CONFLICT (tenant_id, chat_session_id, turn_index) DO NOTHING
                    """  # noqa: S608 - _ROUTING_COLUMNS is a module constant
                ).bindparams(bindparam("candidates", type_=JSONB)),
                {
                    "id": uuid.uuid4(),
                    "tenant_id": tenant_id,
                    "session_id": authoritative,
                    "turn_index": turn_index,
                    "policy_version": decision.policy_version,
                    "agent_version": agent_version,
                    "outcome": decision.outcome.value,
                    "rule": decision.rule.value,
                    "chosen_intent": (
                        decision.chosen.value if decision.chosen is not None else None
                    ),
                    "confidence": decision.confidence,
                    "candidates": [
                        {
                            "intent": candidate.intent.value,
                            "score": candidate.score,
                            "matched_signals": list(candidate.matched_signals),
                        }
                        for candidate in decision.candidates
                    ],
                    "direct_threshold": decision.direct_threshold,
                    "clarify_threshold": decision.clarify_threshold,
                    "conflict_gap": decision.conflict_gap,
                },
            )

    async def current(self, tenant_id: str, session_id: str) -> WorkflowRow | None:
        async with self._engine.begin() as connection:
            authoritative = await _action_session(connection, tenant_id, session_id)
            result = await connection.execute(
                text(
                    f"""
                    SELECT {_WORKFLOW_COLUMNS} FROM agent_workflows
                    WHERE tenant_id = :tenant_id AND chat_session_id = :session_id
                      AND status IN ('active', 'paused')
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """  # noqa: S608 - _WORKFLOW_COLUMNS is a module constant
                ),
                {"tenant_id": tenant_id, "session_id": authoritative},
            )
            row = result.first()
        return _workflow_row(row) if row is not None else None

    async def last_routing(self, tenant_id: str, session_id: str) -> RoutingRow | None:
        async with self._engine.begin() as connection:
            authoritative = await _action_session(connection, tenant_id, session_id)
            result = await connection.execute(
                text(
                    f"""
                    SELECT {_ROUTING_COLUMNS} FROM routing_decisions
                    WHERE tenant_id = :tenant_id AND chat_session_id = :session_id
                    ORDER BY turn_index DESC
                    LIMIT 1
                    """  # noqa: S608 - _ROUTING_COLUMNS is a module constant
                ),
                {"tenant_id": tenant_id, "session_id": authoritative},
            )
            row = result.first()
        return _routing_row(row) if row is not None else None

    async def start(
        self,
        *,
        tenant_id: str,
        session_id: str,
        intent: IntentName,
        agent_version: str,
        next_allowed_actions: tuple[str, ...],
        turn_index: int,
        idempotency_key: IdempotencyKey,
    ) -> WorkflowRow:
        async with self._engine.begin() as connection:
            authoritative = await _action_session(connection, tenant_id, session_id)
            now = datetime.now(UTC)
            inserted = await connection.execute(
                text(
                    f"""
                    INSERT INTO agent_workflows (
                        id, tenant_id, chat_session_id, intent, agent_version,
                        status, collected_fields, pending_confirmation,
                        tool_results, next_allowed_actions, turn_index,
                        created_at, updated_at
                    ) VALUES (
                        :id, :tenant_id, :session_id, :intent, :agent_version,
                        'active', '{{}}'::jsonb, NULL, '[]'::jsonb,
                        :actions, :turn_index, :now, :now
                    )
                    ON CONFLICT (tenant_id, chat_session_id) WHERE status = 'active'
                    DO NOTHING
                    RETURNING {_WORKFLOW_COLUMNS}
                    """
                ).bindparams(bindparam("actions", type_=JSONB)),
                {
                    "id": uuid.uuid4(),
                    "tenant_id": tenant_id,
                    "session_id": authoritative,
                    "intent": intent.value,
                    "agent_version": agent_version,
                    "actions": list(next_allowed_actions),
                    "turn_index": turn_index,
                    "now": now,
                },
            )
            row = inserted.first()
            if row is None:
                # A replayed start: the active workflow this session already
                # has is the one this attempt opened.
                result = await connection.execute(
                    text(
                        f"""
                        SELECT {_WORKFLOW_COLUMNS} FROM agent_workflows
                        WHERE tenant_id = :tenant_id AND chat_session_id = :session_id
                          AND status = 'active'
                        ORDER BY created_at DESC, id DESC
                        LIMIT 1
                        """  # noqa: S608 - _WORKFLOW_COLUMNS is a module constant
                    ),
                    {"tenant_id": tenant_id, "session_id": authoritative},
                )
                row = result.first()
            workflow = _workflow_row(row)
            await self._record_event(
                connection,
                tenant_id=tenant_id,
                workflow_id=_wf_uuid(workflow.workflow_id),
                key=idempotency_key,
                kind="start",
                payload={},
            )
        return workflow

    async def update(
        self,
        *,
        tenant_id: str,
        session_id: str,
        workflow_id: str,
        collected_fields: Mapping[str, str],
        tool_results: tuple[ToolResult, ...],
        next_allowed_actions: tuple[str, ...],
        turn_index: int,
        idempotency_key: IdempotencyKey,
    ) -> WorkflowRow:
        async with self._engine.begin() as connection:
            workflow = await _lock_workflow(connection, tenant_id, session_id, workflow_id)
            uuid_id = _wf_uuid(workflow.workflow_id)
            merged_fields = {**workflow.collected_fields, **collected_fields}
            by_call_id = {result["call_id"]: result for result in workflow.tool_results}
            for result in tool_results:
                by_call_id[result.call_id] = {
                    "call_id": result.call_id,
                    "name": result.name,
                    "result": result.result,
                }
            updated = await connection.execute(
                text(
                    f"""
                    UPDATE agent_workflows
                    SET collected_fields = :fields,
                        tool_results = :results,
                        next_allowed_actions = :actions,
                        turn_index = :turn_index,
                        updated_at = now()
                    WHERE tenant_id = :tenant_id AND id = :workflow_id
                    RETURNING {_WORKFLOW_COLUMNS}
                    """  # noqa: S608 - _WORKFLOW_COLUMNS is a module constant
                ).bindparams(
                    bindparam("fields", type_=JSONB),
                    bindparam("results", type_=JSONB),
                    bindparam("actions", type_=JSONB),
                ),
                {
                    "tenant_id": tenant_id,
                    "workflow_id": uuid_id,
                    "fields": merged_fields,
                    "results": list(by_call_id.values()),
                    "actions": list(next_allowed_actions),
                    "turn_index": turn_index,
                },
            )
            updated_row = updated.one()
            await self._record_event(
                connection,
                tenant_id=tenant_id,
                workflow_id=uuid_id,
                key=idempotency_key,
                kind="update",
                payload={
                    "fields": dict(collected_fields),
                    "results": [result.call_id for result in tool_results],
                },
            )
        return _workflow_row(updated_row)

    async def transition(
        self,
        *,
        tenant_id: str,
        session_id: str,
        workflow_id: str,
        transition: WorkflowTransition,
        payload: Mapping[str, object],
        idempotency_key: IdempotencyKey,
    ) -> WorkflowRow:
        key_hash = _hashed(idempotency_key.value)
        async with self._engine.begin() as connection:
            workflow = await _lock_workflow(connection, tenant_id, session_id, workflow_id)
            uuid_id = _wf_uuid(workflow.workflow_id)
            replayed = await connection.execute(
                text(
                    """
                    SELECT 1 FROM workflow_events
                    WHERE workflow_id = :workflow_id AND key_hash = :key_hash
                    """
                ),
                {"workflow_id": uuid_id, "key_hash": key_hash},
            )
            if replayed.scalar_one_or_none() is not None:
                # The effect already landed; the workflow has moved on, so
                # re-validating the transition would refuse a done thing.
                return workflow
            moved = transition_workflow(
                _row_state(workflow), transition, payload=payload, now=datetime.now(UTC)
            )
            updated = _state_row(moved)
            result = await connection.execute(
                text(
                    f"""
                    UPDATE agent_workflows
                    SET status = :status,
                        pending_confirmation = :pending,
                        updated_at = :now,
                        completed_at = :completed
                    WHERE tenant_id = :tenant_id AND id = :workflow_id
                    RETURNING {_WORKFLOW_COLUMNS}
                    """  # noqa: S608 - _WORKFLOW_COLUMNS is a module constant
                ).bindparams(bindparam("pending", type_=JSONB)),
                {
                    "tenant_id": tenant_id,
                    "workflow_id": uuid_id,
                    "status": updated.status.value,
                    "pending": updated.pending_confirmation,
                    "now": updated.updated_at,
                    "completed": updated.completed_at,
                },
            )
            updated_row = result.one()
            await self._record_event(
                connection,
                tenant_id=tenant_id,
                workflow_id=uuid_id,
                key=idempotency_key,
                kind=transition.value,
                payload=payload,
            )
        return _workflow_row(updated_row)

    async def routing_decisions(self, tenant_id: str, session_id: str) -> tuple[RoutingRow, ...]:
        async with self._engine.begin() as connection:
            authoritative = await _action_session(connection, tenant_id, session_id)
            result = await connection.execute(
                text(
                    f"""
                    SELECT {_ROUTING_COLUMNS} FROM routing_decisions
                    WHERE tenant_id = :tenant_id AND chat_session_id = :session_id
                    ORDER BY turn_index
                    """  # noqa: S608 - _ROUTING_COLUMNS is a module constant
                ),
                {"tenant_id": tenant_id, "session_id": authoritative},
            )
            rows = result.all()
        return tuple(_routing_row(row) for row in rows)

    async def workflows(self, tenant_id: str, session_id: str) -> tuple[WorkflowRow, ...]:
        async with self._engine.begin() as connection:
            authoritative = await _action_session(connection, tenant_id, session_id)
            result = await connection.execute(
                text(
                    f"""
                    SELECT {_WORKFLOW_COLUMNS} FROM agent_workflows
                    WHERE tenant_id = :tenant_id AND chat_session_id = :session_id
                    ORDER BY created_at, id
                    """  # noqa: S608 - _WORKFLOW_COLUMNS is a module constant
                ),
                {"tenant_id": tenant_id, "session_id": authoritative},
            )
            rows = result.all()
        return tuple(_workflow_row(row) for row in rows)

    async def events(self, tenant_id: str, workflow_id: str) -> tuple[WorkflowEventRow, ...]:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT workflow_id, transition, payload, created_at
                    FROM workflow_events
                    WHERE tenant_id = :tenant_id AND workflow_id = :workflow_id
                    ORDER BY created_at, id
                    """
                ),
                {"tenant_id": tenant_id, "workflow_id": _wf_uuid(workflow_id)},
            )
            rows = result.all()
        return tuple(_event_row(row) for row in rows)

    async def _record_event(
        self,
        connection: AsyncConnection,
        *,
        tenant_id: str,
        workflow_id: uuid.UUID,
        key: IdempotencyKey,
        kind: str,
        payload: Mapping[str, object],
    ) -> None:
        await connection.execute(
            text(
                """
                INSERT INTO workflow_events (
                    id, tenant_id, workflow_id, transition, key_hash, payload, created_at
                ) VALUES (
                    :id, :tenant_id, :workflow_id, :transition, :key_hash, :payload, now()
                )
                ON CONFLICT (workflow_id, key_hash) DO NOTHING
                """
            ).bindparams(bindparam("payload", type_=JSONB)),
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_id,
                "workflow_id": workflow_id,
                "transition": kind,
                "key_hash": _hashed(key.value),
                "payload": dict(payload),
            },
        )


async def _lock_workflow(
    connection: AsyncConnection, tenant_id: str, session_id: str, workflow_id: str
) -> WorkflowRow:
    """The session's workflow row, locked, or a tenant-scoped refusal.

    The session is resolved the same way the writes resolve it, so a workflow
    started under the resolved row is found when the caller names the session
    the API issued. The session resolution may create a row for a
    runtime-level session; that is the same side effect the writes have.
    """
    authoritative = await _action_session(connection, tenant_id, session_id)
    row = await connection.execute(
        text(
            f"""
            SELECT {_WORKFLOW_COLUMNS} FROM agent_workflows
            WHERE tenant_id = :tenant_id AND chat_session_id = :session_id
              AND id = :workflow_id
            FOR UPDATE
            """  # noqa: S608 - _WORKFLOW_COLUMNS is a module constant
        ),
        {
            "tenant_id": tenant_id,
            "session_id": authoritative,
            "workflow_id": _wf_uuid(workflow_id),
        },
    )
    locked = row.first()
    if locked is None:
        raise NotFoundError(detail="workflow absent or outside tenant")
    return _workflow_row(locked)
