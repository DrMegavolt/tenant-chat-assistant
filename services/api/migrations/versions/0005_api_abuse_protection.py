"""Shared rate-limit counters for the SEC-003 guards.

Revision ID: 0005_api_abuse_protection
Revises: 0004_agent_runtime
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_api_abuse_protection"
down_revision: str | None = "0004_agent_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the counter table every replica's rate limits share.

    Rows are per (key, window) and are deleted by the sweep that runs on each
    hit, so the table holds at most the identities active in the last couple of
    windows rather than an archive. The window index keeps that sweep on an
    index when the table is large.
    """
    op.execute(
        """
        CREATE TABLE rate_limit_counters (
            scope_key text NOT NULL,
            window_start bigint NOT NULL,
            count integer NOT NULL,
            PRIMARY KEY (scope_key, window_start)
        );
        CREATE INDEX idx_rate_limit_counters_window
            ON rate_limit_counters (window_start);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX idx_rate_limit_counters_window;
        DROP TABLE rate_limit_counters;
        """
    )
