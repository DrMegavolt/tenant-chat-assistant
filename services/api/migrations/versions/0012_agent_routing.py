"""The durable routing and workflow records (`AGENT-001`).

Revision ID: 0012_agent_routing
Revises: 0011_ingestion_generations
Create Date: 2026-08-05
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012_agent_routing"
down_revision: str | None = "0011_ingestion_generations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Persist what the router decided and what the workflow became.

    The three tables are the system of record the checkpoint is not:

    - ``routing_decisions`` is append-only per turn, keyed by
      ``(tenant_id, chat_session_id, turn_index)`` so a replayed route node
      rewrites its own row instead of duplicating it. It carries the whole
      decision — every candidate with its score, the chosen intent, the
      confidence, the policy version, and the thresholds applied — so a
      misrouted turn is diagnosable from this table alone (`OBS-004`).
    - ``agent_workflows`` holds the active workflow: the intent, the collected
      fields, the pending confirmation, the tool results, and the next allowed
      actions. The partial unique index allows exactly one ``active`` workflow
      per session, which is what a replayed ``start`` collides with.
    - ``workflow_events`` is the transition log. Each event is keyed by the
      digest of its idempotency key, so a replayed transition never records
      twice; ``ck_events_payload_object`` bounds the payload the same way the
      findings payload is bounded elsewhere — counts, names, and decisions,
      never document content.
    """
    op.execute(
        """
        CREATE TYPE routing_outcome AS ENUM ('direct', 'clarify', 'handoff');
        CREATE TYPE workflow_status AS ENUM (
            'active', 'paused', 'completed', 'cancelled', 'suspended',
            'failed', 'handed_off'
        );

        CREATE TABLE routing_decisions (
            id uuid PRIMARY KEY,
            tenant_id varchar(63) NOT NULL,
            chat_session_id uuid NOT NULL,
            turn_index integer NOT NULL,
            policy_version varchar(100) NOT NULL,
            agent_version varchar(100) NOT NULL DEFAULT '',
            outcome routing_outcome NOT NULL,
            rule varchar(63) NOT NULL,
            chosen_intent varchar(63),
            confidence double precision NOT NULL,
            candidates jsonb NOT NULL,
            direct_threshold double precision NOT NULL,
            clarify_threshold double precision NOT NULL,
            conflict_gap double precision NOT NULL,
            created_at timestamptz NOT NULL,
            CONSTRAINT fk_decisions_session
                FOREIGN KEY (tenant_id, chat_session_id)
                REFERENCES chat_sessions (tenant_id, id) ON DELETE CASCADE,
            CONSTRAINT uq_decisions_session_turn
                UNIQUE (tenant_id, chat_session_id, turn_index),
            CONSTRAINT ck_decisions_rule_format
                CHECK (rule ~ '^[a-z][a-z0-9_.-]{0,62}$'),
            CONSTRAINT ck_decisions_candidates_object
                CHECK (jsonb_typeof(candidates) = 'array')
        );

        CREATE INDEX ix_decisions_tenant_created
            ON routing_decisions (tenant_id, created_at DESC);

        CREATE TABLE agent_workflows (
            id uuid PRIMARY KEY,
            tenant_id varchar(63) NOT NULL,
            chat_session_id uuid NOT NULL,
            intent varchar(63) NOT NULL,
            agent_version varchar(100) NOT NULL,
            status workflow_status NOT NULL,
            collected_fields jsonb NOT NULL DEFAULT '{}'::jsonb,
            pending_confirmation jsonb,
            tool_results jsonb NOT NULL DEFAULT '[]'::jsonb,
            next_allowed_actions jsonb NOT NULL DEFAULT '[]'::jsonb,
            turn_index integer NOT NULL,
            created_at timestamptz NOT NULL,
            updated_at timestamptz NOT NULL,
            completed_at timestamptz,
            CONSTRAINT fk_workflows_session
                FOREIGN KEY (tenant_id, chat_session_id)
                REFERENCES chat_sessions (tenant_id, id) ON DELETE CASCADE,
            CONSTRAINT uq_workflows_tenant_id UNIQUE (tenant_id, id),
            CONSTRAINT ck_workflows_collected_object
                CHECK (jsonb_typeof(collected_fields) = 'object'),
            CONSTRAINT ck_workflows_results_array
                CHECK (jsonb_typeof(tool_results) = 'array'),
            CONSTRAINT ck_workflows_actions_array
                CHECK (jsonb_typeof(next_allowed_actions) = 'array'),
            CONSTRAINT ck_workflows_terminal_completed CHECK
                ((status IN ('completed', 'cancelled', 'suspended', 'failed', 'handed_off'))
                 = (completed_at IS NOT NULL))
        );

        CREATE INDEX ix_workflows_tenant_updated
            ON agent_workflows (tenant_id, updated_at DESC);

        -- A partial UNIQUE must be an index, not an inline constraint: this is
        -- what a replayed `start` collides with, keeping one active workflow
        -- per session without barring the finished rows.
        CREATE UNIQUE INDEX uq_workflows_active_session
            ON agent_workflows (tenant_id, chat_session_id) WHERE status = 'active';

        CREATE TABLE workflow_events (
            id uuid PRIMARY KEY,
            tenant_id varchar(63) NOT NULL,
            workflow_id uuid NOT NULL,
            transition varchar(63) NOT NULL,
            key_hash varchar(64) NOT NULL,
            payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL,
            CONSTRAINT fk_events_workflow
                FOREIGN KEY (tenant_id, workflow_id)
                REFERENCES agent_workflows (tenant_id, id) ON DELETE CASCADE,
            CONSTRAINT uq_events_workflow_key UNIQUE (workflow_id, key_hash),
            CONSTRAINT ck_events_transition_format
                CHECK (transition ~ '^[a-z][a-z0-9_.-]{0,62}$'),
            CONSTRAINT ck_events_key_hash CHECK (key_hash ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_events_payload_object CHECK (jsonb_typeof(payload) = 'object')
        );

        CREATE INDEX ix_events_workflow_created
            ON workflow_events (tenant_id, workflow_id, created_at);
        """
    )


def downgrade() -> None:
    """Remove the routing and workflow records; conversations keep their transcript."""
    op.execute(
        """
        DROP TABLE workflow_events;
        DROP TABLE agent_workflows;
        DROP TABLE routing_decisions;
        DROP TYPE workflow_status;
        DROP TYPE routing_outcome;
        """
    )
