"""The `FEAT-008` review queue: visitor feedback, review cases, and the
reviewer's diagnosis overlay.

Revision ID: 0016_review_queue
Revises: 0015_diagnosis_status
Create Date: 2026-08-06
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0016_review_queue"
down_revision: str | None = "0015_diagnosis_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the review surface; the trace itself stays immutable.

    ``turn_feedback`` is one row per turn record — the visitor's rating and an
    optional reason, idempotently upserted so re-rating replaces rather than
    stacks. ``review_queue`` is one case per turn: it carries the content-free
    priority inputs (severity inputs, recurrence, business outcome, manifest
    novelty), the reviewer's verdict and corrected answer, and the
    fix-closure reference an evaluation run populates (acceptance 5).
    ``review_diagnoses`` holds the reviewer's overlay rows: each one confirms,
    rejects, amends, or adds beside the detector's records in the turn's
    opaque content, so automatic and reviewer diagnoses stay distinct and a
    disagreement is a stored relationship, never a silent rewrite (acceptance
    4). Every review table cascades off its turn record, so the `PRIV-002`
    erasure statement that deletes a turn removes its feedback, review, and
    decisions in the same transaction — the feedback reason is the visitor's
    words and must not outlive their erasure.

    ``turn_record_projections.payload`` holds the promoted evaluation case
    (`acceptance 6`): the anonymized payload the harness loads, pinned to the
    turn it was derived from exactly like the projection itself.
    """
    op.execute(
        """
        ALTER TABLE turn_record_projections
            ADD COLUMN payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            ADD CONSTRAINT ck_turn_record_projections_payload_object CHECK
                (jsonb_typeof(payload) = 'object');

        CREATE TABLE turn_feedback (
            id uuid PRIMARY KEY,
            tenant_id varchar(63) NOT NULL,
            turn_record_id uuid NOT NULL,
            rating varchar(8) NOT NULL,
            reason text,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_turn_feedback_tenant FOREIGN KEY (tenant_id)
                REFERENCES tenants (id) ON DELETE CASCADE,
            CONSTRAINT fk_turn_feedback_turn FOREIGN KEY (tenant_id, turn_record_id)
                REFERENCES turn_records (tenant_id, id) ON DELETE CASCADE,
            CONSTRAINT uq_turn_feedback_turn UNIQUE (tenant_id, turn_record_id),
            CONSTRAINT ck_turn_feedback_rating CHECK (rating IN ('up', 'down')),
            CONSTRAINT ck_turn_feedback_reason CHECK
                (reason IS NULL OR length(btrim(reason)) > 0)
        );

        CREATE TABLE review_queue (
            id uuid PRIMARY KEY,
            tenant_id varchar(63) NOT NULL,
            turn_record_id uuid NOT NULL,
            source varchar(16) NOT NULL,
            status varchar(16) NOT NULL DEFAULT 'open',
            priority integer NOT NULL,
            recurrence integer NOT NULL DEFAULT 1,
            manifest_hash varchar(64) NOT NULL,
            committed_actions boolean NOT NULL DEFAULT false,
            novel_manifest boolean NOT NULL DEFAULT false,
            case_id varchar(128),
            reviewer_subject varchar(200),
            reviewed_at timestamptz,
            verdict varchar(16),
            verdict_note text,
            corrected_answer text,
            proposed_fix text,
            closing_eval_run_id varchar(128),
            closing_eval_case_id varchar(128),
            closing_eval_passed_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_review_queue_tenant FOREIGN KEY (tenant_id)
                REFERENCES tenants (id) ON DELETE CASCADE,
            CONSTRAINT fk_review_queue_turn FOREIGN KEY (tenant_id, turn_record_id)
                REFERENCES turn_records (tenant_id, id) ON DELETE CASCADE,
            CONSTRAINT uq_review_queue_turn UNIQUE (tenant_id, turn_record_id),
            CONSTRAINT uq_review_queue_tenant_id UNIQUE (tenant_id, id),
            CONSTRAINT ck_review_queue_source CHECK
                (source IN ('user_feedback', 'automatic')),
            CONSTRAINT ck_review_queue_status CHECK
                (status IN ('open', 'in_review', 'awaiting_fix', 'rejected', 'resolved')),
            CONSTRAINT ck_review_queue_priority CHECK
                (priority >= 0 AND priority <= 100),
            CONSTRAINT ck_review_queue_recurrence CHECK (recurrence >= 1),
            CONSTRAINT ck_review_queue_manifest_hash CHECK
                (manifest_hash ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_review_queue_reviewer_subject CHECK
                (reviewer_subject IS NULL OR btrim(reviewer_subject) <> ''),
            CONSTRAINT ck_review_queue_verdict CHECK
                (verdict IN ('confirmed', 'rejected', 'amended')),
            CONSTRAINT ck_review_queue_verdict_note CHECK
                (verdict_note IS NULL OR length(btrim(verdict_note)) > 0),
            CONSTRAINT ck_review_queue_corrected_answer CHECK
                (corrected_answer IS NULL OR length(btrim(corrected_answer)) > 0),
            CONSTRAINT ck_review_queue_proposed_fix CHECK
                (proposed_fix IS NULL OR length(btrim(proposed_fix)) > 0),
            CONSTRAINT ck_review_queue_case_id CHECK
                (case_id IS NULL OR length(btrim(case_id)) > 0),
            CONSTRAINT ck_review_queue_closing_run CHECK
                (closing_eval_run_id IS NULL OR length(btrim(closing_eval_run_id)) > 0),
            CONSTRAINT ck_review_queue_closing_case CHECK
                (closing_eval_case_id IS NULL OR length(btrim(closing_eval_case_id)) > 0)
        );

        CREATE INDEX ix_review_queue_tenant_status
            ON review_queue (tenant_id, status, priority DESC, created_at);
        CREATE INDEX ix_review_queue_tenant_manifest
            ON review_queue (tenant_id, manifest_hash);
        CREATE INDEX ix_review_queue_tenant_case
            ON review_queue (tenant_id, case_id);

        CREATE TABLE review_diagnoses (
            id uuid PRIMARY KEY,
            tenant_id varchar(63) NOT NULL,
            review_id uuid NOT NULL,
            relationship varchar(16) NOT NULL,
            automatic_index integer,
            cause varchar(64) NOT NULL,
            stage varchar(64) NOT NULL,
            role varchar(16) NOT NULL,
            status varchar(16) NOT NULL,
            confidence varchar(8) NOT NULL,
            evidence text[] NOT NULL DEFAULT '{}',
            note text,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_review_diagnoses_tenant FOREIGN KEY (tenant_id)
                REFERENCES tenants (id) ON DELETE CASCADE,
            CONSTRAINT fk_review_diagnoses_review FOREIGN KEY (tenant_id, review_id)
                REFERENCES review_queue (tenant_id, id) ON DELETE CASCADE,
            CONSTRAINT ck_review_diagnoses_relationship CHECK
                (relationship IN ('confirms', 'rejects', 'amends', 'adds')),
            CONSTRAINT ck_review_diagnoses_index CHECK
                (automatic_index IS NULL OR automatic_index >= 0),
            CONSTRAINT ck_review_diagnoses_role CHECK
                (role IN ('primary', 'contributing')),
            CONSTRAINT ck_review_diagnoses_status CHECK
                (status IN ('detected', 'suspected', 'confirmed', 'inconclusive')),
            CONSTRAINT ck_review_diagnoses_confidence CHECK
                (confidence IN ('low', 'medium', 'high')),
            CONSTRAINT ck_review_diagnoses_index_use CHECK
                ((relationship = 'adds') = (automatic_index IS NULL)),
            CONSTRAINT ck_review_diagnoses_note CHECK
                (note IS NULL OR length(btrim(note)) > 0)
        );

        CREATE INDEX ix_review_diagnoses_review
            ON review_diagnoses (tenant_id, review_id);
        """
    )


def downgrade() -> None:
    """Remove the review surface; turn records and projections are untouched
    except the payload column, which drops with the projection's contract."""
    op.execute(
        """
        DROP TABLE review_diagnoses;
        DROP TABLE review_queue;
        DROP TABLE turn_feedback;

        ALTER TABLE turn_record_projections
            DROP CONSTRAINT ck_turn_record_projections_payload_object,
            DROP COLUMN payload;
        """
    )
