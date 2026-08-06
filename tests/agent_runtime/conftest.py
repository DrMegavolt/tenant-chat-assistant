"""A whole agent runtime with no model, no database, and no network.

The graph runs against the *real* idempotent services from
:mod:`tenantchat.api.actions`, backed by the in-memory stores. That combination
is the point: an ARCH-001 acceptance criterion about replay is only worth
anything if the thing being replayed is the code that will run in production,
and only the store underneath it is a fake.

The model is scripted rather than recorded. A recorded fixture would pin these
tests to one provider's phrasing, and every assertion here is about the runtime's
behavior — what it commits, when it pauses, what survives a restart — none of
which should change when the model does.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Collection, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

from tenantchat.api.agent import build_dispatch_dependencies, build_dispatch_runtime
from tenantchat.api.registry import TenantRegistry, demo_offered_slots
from tenantchat.api.store import (
    ConsentRecord,
    ConsentStore,
    InMemoryBookingStore,
    InMemoryHandoffStore,
    InMemoryIdempotencyStore,
    InMemoryLeadStore,
    InMemoryWorkflowStore,
    WorkflowStore,
)
from tenantchat.core.ports import EvidenceSource
from tenantchat.core.privacy import ConsentGrant, ConsentPurpose
from tenantchat.orchestration.checkpoints import Checkpointer, InMemorySaver
from tenantchat.orchestration.dependencies import DispatchDependencies
from tenantchat.orchestration.model import (
    AssembledPrompt,
    ModelResponse,
    ToolCall,
    ToolSpec,
)
from tenantchat.orchestration.runtime import DispatchRuntime
from tenantchat.orchestration.state import DispatchState

BOOKING_TENANT = "clearview"
LEAD_TENANT = "apex"
# Drawn from the same future window the demo provider offers, so a scripted
# booking names a slot the domain will accept (a fixed past date would be
# refused by the "not in the past" rule).
OFFERED_SLOT = demo_offered_slots("hvac")[0].label


def tool_call(name: str, **arguments: object) -> ToolCall:
    """One tool call, with a call ID derived from the tool it invokes.

    Derived rather than passed so a script with two calls in one response cannot
    accidentally share an ID — which would make two actions derive one
    idempotency key and quietly collapse into one.
    """
    return ToolCall(call_id=f"call-{name}", name=name, arguments=arguments)


def booking_arguments() -> dict[str, object]:
    return {
        "service": "HVAC",
        "slot": OFFERED_SLOT,
        "customer_name": "Dana Ruiz",
        "customer_phone_or_email": "555-222-1919",
        "address": "12 Alder Court, Portland, OR 97205",
    }


@dataclass
class ScriptedModel:
    """Replays a fixed list of responses, then repeats the last one.

    Repeating rather than raising at the end of the script keeps a test that
    exercises the round budget from having to enumerate every call the graph
    will make; the loop guard is what the test is about, not the script length.
    """

    script: list[ModelResponse]
    calls: list[AssembledPrompt] = field(default_factory=list)
    # The allowlist each call was offered, one entry per call. The graph's
    # permission boundary is "the model only ever sees its agent's tools", and
    # an assertion needs the offered set, not the prompt that carried it.
    offered_tools: list[tuple[str, ...]] = field(default_factory=list)
    failure: Exception | None = None

    async def complete(
        self,
        prompt: AssembledPrompt,
        *,
        tools: Sequence[ToolSpec],
    ) -> ModelResponse:
        self.calls.append(prompt)
        self.offered_tools.append(tuple(spec.name for spec in tools))
        if self.failure is not None:
            raise self.failure
        index = min(len(self.calls) - 1, len(self.script) - 1)
        return self.script[index]

    @property
    def call_count(self) -> int:
        return len(self.calls)


class CountingSaver(InMemorySaver):
    """An :class:`InMemorySaver` that records how many checkpoints it wrote.

    A subclass because LangGraph type-checks the checkpointer it is handed. Both
    write paths are counted: ``aput`` persists a superstep's state and
    ``aput_writes`` persists a node's pending output, and the budget in
    `ADR-0001` is about round trips to PostgreSQL, which is what both become.
    """

    def __init__(self) -> None:
        super().__init__()
        # Not `writes`: the base class already owns that name for its pending
        # writes table, and shadowing it corrupts the saver rather than failing.
        self.checkpoint_writes = 0

    async def aput(self, *args: Any, **kwargs: Any) -> Any:
        self.checkpoint_writes += 1
        return await super().aput(*args, **kwargs)

    async def aput_writes(self, *args: Any, **kwargs: Any) -> None:
        self.checkpoint_writes += 1
        await super().aput_writes(*args, **kwargs)


@dataclass
class RuntimeHarness:
    """One runtime plus the stores it writes to, so a test can inspect both."""

    runtime: DispatchRuntime
    dependencies: DispatchDependencies
    model: ScriptedModel
    bookings: InMemoryBookingStore
    leads: InMemoryLeadStore
    handoffs: InMemoryHandoffStore
    idempotency: InMemoryIdempotencyStore
    consent: ConsentStore
    workflows: WorkflowStore
    checkpointer: Checkpointer

    async def checkpointed_state(self, tenant_id: str, session_id: str) -> DispatchState:
        """The state a resumed process would pick up."""
        return cast("DispatchState", dict(await self.runtime.snapshot(tenant_id, session_id)))

    def restarted(self) -> RuntimeHarness:
        """The same conversation seen by a freshly started process.

        Everything except the checkpointer and the stores is rebuilt: a new
        graph, new nodes, new service instances. What survives is exactly what a
        deployment would leave behind, which is what makes this a restart rather
        than a second call on the same objects.

        The new model picks the script up where the old one stopped. A real
        provider has no memory of the calls the previous process made, and
        replaying the script from the top would have the restarted assistant
        propose the booking it is in the middle of confirming.
        """
        remaining = self.model.script[self.model.call_count :]
        return build_harness(
            remaining or self.model.script[-1:],
            checkpointer=self.checkpointer,
            bookings=self.bookings,
            leads=self.leads,
            handoffs=self.handoffs,
            idempotency=self.idempotency,
            workflows=self.workflows,
        )


class PermissiveConsentStore:
    """Consent that is always granted, for the graph-behavior suite.

    These tests pin what the runtime commits and replays, not the consent gate
    (`PRIV-001` tests that separately against the real store). Every session is
    treated as having granted every purpose, so a missing grant can never mask
    the durability behavior under test.
    """

    async def record(
        self,
        tenant_id: str,
        session_id: str,
        *,
        purposes: Collection[ConsentPurpose],
        statement: str,
    ) -> tuple[ConsentRecord, ...]:
        del tenant_id, session_id, purposes, statement
        return ()

    async def consent_grant(self, tenant_id: str, session_id: str) -> ConsentGrant:
        return ConsentGrant(
            tenant_id=tenant_id,
            session_id=session_id,
            purposes=frozenset(ConsentPurpose),
            statement="test",
            granted_at=datetime.now(UTC),
        )

    async def for_session(self, tenant_id: str, session_id: str) -> tuple[ConsentRecord, ...]:
        del tenant_id, session_id
        return ()


def build_harness(
    script: list[ModelResponse],
    *,
    checkpointer: Checkpointer | None = None,
    bookings: InMemoryBookingStore | None = None,
    leads: InMemoryLeadStore | None = None,
    handoffs: InMemoryHandoffStore | None = None,
    idempotency: InMemoryIdempotencyStore | None = None,
    consent: ConsentStore | None = None,
    workflows: WorkflowStore | None = None,
    evidence: EvidenceSource | None = None,
) -> RuntimeHarness:
    """Compose a runtime over in-memory adapters."""
    model = ScriptedModel(script=list(script))
    booking_store = bookings if bookings is not None else InMemoryBookingStore()
    lead_store = leads if leads is not None else InMemoryLeadStore()
    handoff_store = handoffs if handoffs is not None else InMemoryHandoffStore()
    key_store = idempotency if idempotency is not None else InMemoryIdempotencyStore()
    consent_store = consent if consent is not None else PermissiveConsentStore()
    workflow_store = workflows if workflows is not None else InMemoryWorkflowStore()
    saver = checkpointer if checkpointer is not None else InMemorySaver()

    adapters: dict[str, Any] = {
        "registry": TenantRegistry.seeded(),
        "model": model,
        "bookings": booking_store,
        "leads": lead_store,
        "handoffs": handoff_store,
        "idempotency": key_store,
        "consent": consent_store,
        "workflows": workflow_store,
        "evidence": evidence,
    }
    runtime = build_dispatch_runtime(**adapters, checkpointer=saver)
    return RuntimeHarness(
        runtime=runtime,
        # A second, equivalent bundle over the same stores, for tests that drive
        # a node directly. Separate service instances are the point: idempotency
        # that lived on an instance rather than in the store would pass the
        # graph-level tests and fail this one.
        dependencies=build_dispatch_dependencies(**adapters),
        model=model,
        bookings=booking_store,
        leads=lead_store,
        handoffs=handoff_store,
        idempotency=key_store,
        consent=consent_store,
        workflows=workflow_store,
        checkpointer=saver,
    )


def tool_results(harness: RuntimeHarness) -> list[dict[str, object]]:
    """Every tool payload the graph sent back to the model, in order."""
    payloads = []
    for prompt in harness.model.calls:
        for message in prompt.messages:
            if message.role == "tool":
                parsed = json.loads(message.content)
                if parsed not in payloads:
                    payloads.append(parsed)
    return payloads


@pytest.fixture
def answering_model() -> Iterator[list[ModelResponse]]:
    """A model that answers in one call and asks for nothing."""
    yield [ModelResponse(content="We are open until 7pm.", model_name="scripted")]


@pytest.fixture
def booking_model() -> Iterator[list[ModelResponse]]:
    """A model that proposes a valid booking, then confirms it in prose."""
    yield [
        ModelResponse(
            content="",
            tool_calls=(tool_call("book_appointment", **booking_arguments()),),
            model_name="scripted",
        ),
        ModelResponse(content="You are booked for Monday at 2pm.", model_name="scripted"),
    ]


# --- PostgreSQL fixtures for the integration suite ------------------------
#
# A container of its own, with its own role, matching `tests/migrations` and
# `tests/repositories`. Sharing one server across suites would make a failure in
# any of them depend on what the others had already done to it, which is the
# property these disposable databases exist to remove.

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def agent_postgres_server_url() -> Iterator[str]:
    with PostgresContainer(
        "postgres:16.11-alpine3.23@sha256:4327b9fd295502f326f44153a1045a7170ddbfffed1c3829798328556cfd09e2",
        username="agent_owner",
        password="agent-test-only",
        dbname="postgres",
        driver="psycopg",
    ) as postgres:
        yield postgres.get_connection_url()


@pytest.fixture
def repository_database_url(agent_postgres_server_url: str) -> Iterator[str]:
    """A disposable database at the current Alembic head."""
    server = agent_postgres_server_url
    database_name = f"agent_{uuid.uuid4().hex}"
    with psycopg.connect(_libpq(server), autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))

    database_url = server.rsplit("/", maxsplit=1)[0] + f"/{database_name}"
    os.environ["DATABASE_MIGRATION_URL"] = database_url
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "services/api/migrations"))
    command.upgrade(config, "head")
    try:
        yield database_url
    finally:
        with psycopg.connect(_libpq(server), autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database_name))
            )


@pytest.fixture
def agent_database_url(repository_database_url: str) -> Iterator[str]:
    """A migrated database with the checkpoint schema created the deployed way.

    Calling the same entry point ``make migrate-checkpoints`` runs, rather than
    ``saver.setup()`` inline, so a change that breaks the operational path fails
    here instead of in production.
    """
    from scripts.setup_checkpoints import _setup

    asyncio.run(_setup(repository_database_url))
    yield repository_database_url


def _libpq(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)
