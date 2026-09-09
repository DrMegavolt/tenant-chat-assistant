"""Export or verify the complete HTTP contract without starting dependencies."""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

from tenantchat.api.app import create_app
from tenantchat.api.settings import Settings
from tenantchat.api.store import (
    InMemoryAuditStore,
    InMemoryBookingStore,
    InMemoryConsentStore,
    InMemoryConversationStore,
    InMemoryHandoffStore,
    InMemoryIdempotencyStore,
    InMemoryLeadStore,
    InMemoryMembershipStore,
    InMemoryPrivacyStore,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "openapi.json"


def render_contract() -> str:
    conversations = InMemoryConversationStore()
    bookings = InMemoryBookingStore()
    leads = InMemoryLeadStore()
    handoffs = InMemoryHandoffStore()
    consent = InMemoryConsentStore()
    app = create_app(
        Settings(allowed_origins=(), max_request_bytes=2048, docs_enabled=True),
        booking_store=bookings,
        lead_store=leads,
        conversation_store=conversations,
        handoff_store=handoffs,
        consent_store=consent,
        idempotency_store=InMemoryIdempotencyStore(),
        membership_store=InMemoryMembershipStore(),
        audit_store=InMemoryAuditStore(),
        privacy_store=InMemoryPrivacyStore(conversations, bookings, leads, handoffs, consent),
    )
    return json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    args = parser.parse_args()
    actual = render_contract()
    if args.write:
        CONTRACT.parent.mkdir(parents=True, exist_ok=True)
        CONTRACT.write_text(actual, encoding="utf-8")
        return 0
    if not CONTRACT.is_file():
        sys.stderr.write("Missing contracts/openapi.json; see make contract-update.\n")
        return 1
    expected = CONTRACT.read_text(encoding="utf-8")
    if actual == expected:
        return 0
    sys.stderr.writelines(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            actual.splitlines(keepends=True),
            fromfile="contracts/openapi.json (reviewed)",
            tofile="runtime OpenAPI",
        )
    )
    sys.stderr.write("API drift: review the specification before make contract-update.\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
