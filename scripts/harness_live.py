#!/usr/bin/env python3
"""Live semantic acceptance checks against the cluster's configured model.

This is the demo seed — it puts real records in front of the explorer on the day.
Idempotent and re-runnable: every case opens a fresh session, sends a visitor message,
and prints the answer, turn id, and outcome. Next-run picks a new session and
produces a new set of records.

Environment:
    CHAT_API_URL          visitor-facing widget API root (default http://localhost:8004)
    HARNESS_TIMEOUT       per-request HTTP timeout in seconds (default 60)

Usage:
    uv run --frozen python scripts/harness_live.py

The script requires the seed knowledge to have been loaded already
(``make seed-knowledge``) so the retrieval pipeline has evidence to find.
"""

from __future__ import annotations

import http.client
import json
import os
import uuid
from typing import Any, cast
from urllib.parse import SplitResult, urlsplit


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


CHAT_API_URL = _env("CHAT_API_URL", "http://localhost:8004")
TIMEOUT = int(_env("HARNESS_TIMEOUT", "60"))

HARNESS_TENANTS = ("clearview", "apex")

HEALTH_PATH = "/readyz"

EXIT_SUCCESS = 0
EXIT_FAILURE = 1

VISITOR_CREDENTIAL_HEADER = "X-Visitor-Credential"

CASES: tuple[dict[str, Any], ...] = (
    {
        "id": "financing-options",
        "query": "What financing options are available for a major HVAC replacement?",
        "label": "grounded financing answer",
        "outcomes": ("answered",),
        "min_citations": 1,
    },
    {
        "id": "financing-qualification",
        "query": "Can you guarantee my loan approval and exact monthly payment?",
        "label": "grounded qualification limitation",
        "outcomes": ("answered",),
        "min_citations": 1,
    },
    {
        "id": "small-visit",
        "query": "Does financing normally cover a small diagnostic visit?",
        "label": "grounded small-visit limitation",
        "outcomes": ("answered",),
        "min_citations": 1,
    },
    {
        "id": "unsupported",
        "query": "Is there a discount for quarterly window cleaning?",
        "label": "unsupported claim abstains",
        "outcomes": ("abstained",),
        "min_citations": 0,
    },
)


def report(line: str) -> None:
    print(line, flush=True)  # noqa: T201


def _parsed_base(url: str) -> SplitResult:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError(f"API URL must be an absolute http(s) URL: {url!r}")
    return parsed


def _connection(base: str) -> http.client.HTTPConnection:
    parsed = _parsed_base(base)
    hostname = cast(str, parsed.hostname)  # Guaranteed by _parsed_base.
    connection_type = (
        http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    )
    return connection_type(hostname, parsed.port, timeout=TIMEOUT)


def _request_path(base: str, path: str) -> str:
    prefix = _parsed_base(base).path.rstrip("/")
    return f"{prefix}/{path.lstrip('/')}"


def _api_post(
    path: str,
    payload: dict[str, Any],
    *,
    base: str = CHAT_API_URL,
    headers_extra: dict[str, str] | None = None,
) -> dict[str, Any]:
    headers: dict[str, str] = {}
    if headers_extra:
        headers.update(headers_extra)
    conn = _connection(base)
    try:
        conn.request(
            "POST",
            _request_path(base, path),
            body=json.dumps(payload),
            headers={**headers, "Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        body = json.loads(resp.read().decode())
        if resp.status >= 400:
            report(f"  POST {path} -> {resp.status}: {json.dumps(body, indent=2)}")
        return cast(dict[str, Any], body)
    finally:
        conn.close()


def _verify_health() -> None:
    report("=== L9b HARNESS-B Live Mode ===")
    report("")
    report(f"  Chat API:   {CHAT_API_URL}")
    report("")
    try:
        conn = _connection(CHAT_API_URL)
        conn.request("GET", _request_path(CHAT_API_URL, HEALTH_PATH))
        resp = conn.getresponse()
        body = json.loads(resp.read().decode())
        if resp.status != 200:
            report(f"FATAL: health check failed: {resp.status} {body}")
            raise SystemExit(2)
        report(f"  Health:     {body.get('status', 'unknown')}")
        conn.close()
    except Exception as exc:
        report(f"FATAL: cannot reach chat API at {CHAT_API_URL}: {exc}")
        raise SystemExit(2) from exc
    report("")


def _open_session(tenant_id: str) -> tuple[str, str]:
    body = _api_post("/api/chat/session", {"tenant_id": tenant_id}, base=CHAT_API_URL)
    credential = str(body["credential"])
    session_id = str(body["session"]["session_id"])
    _api_post(
        "/api/chat/consent",
        {"purposes": ["booking", "follow_up"]},
        base=CHAT_API_URL,
        headers_extra={VISITOR_CREDENTIAL_HEADER: credential},
    )
    return session_id, credential


def _send_message(credential: str, message: str) -> dict[str, Any]:
    return _api_post(
        "/api/chat",
        {"message": message},
        base=CHAT_API_URL,
        headers_extra={VISITOR_CREDENTIAL_HEADER: credential},
    )


def _validate_turn(case: dict[str, Any], turn: dict[str, Any]) -> None:
    outcome = str(turn.get("outcome", ""))
    expected = tuple(str(item) for item in case["outcomes"])
    if outcome not in expected:
        raise RuntimeError(f"expected outcome in {expected}, got {outcome or 'missing'}")
    reply = turn.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        raise RuntimeError("response carried no non-empty reply")
    turn_id = turn.get("turn_id")
    if not isinstance(turn_id, str) or not turn_id:
        raise RuntimeError("response carried no turn_id")
    citations = turn.get("citations")
    if not isinstance(citations, list):
        raise RuntimeError("response citations are not a list")
    minimum = int(case["min_citations"])
    if len(citations) < minimum:
        raise RuntimeError(f"expected at least {minimum} citation(s), got {len(citations)}")


def run_cases() -> int:
    _verify_health()

    errors = 0

    for tenant_id in HARNESS_TENANTS:
        report("─" * 60)
        report(f"Running live semantic checks — tenant: {tenant_id}")
        report("─" * 60)

        for case in CASES:
            case_id = case["id"]
            query = case["query"]
            label = case["label"]
            run_id = uuid.uuid4().hex[:8]

            report(f"\n[{case_id}] {label}")
            report(f"  query: {query}")
            report(f"  run:   {run_id}")

            try:
                session_id, credential = _open_session(tenant_id)
                report(f"  session: {session_id[:8]}...")

                turn = _send_message(credential, query)
                _validate_turn(case, turn)
                reply = turn.get("reply", "")
                turn_id = turn.get("turn_id")
                outcome = turn.get("outcome", "unknown")
                citations = turn.get("citations", [])
                pending = turn.get("pending")

                report(f"  reply:   {reply[:200]}{'...' if len(reply) > 200 else ''}")
                report(f"  turn_id: {turn_id}")
                report(f"  outcome: {outcome}")
                if citations:
                    report(f"  citations: {len(citations)}")
                if pending:
                    report(f"  pending:  {pending.get('awaiting', 'unknown')}")
            except Exception as exc:
                report(f"  ERROR: {exc}")
                errors += 1

        report("")

    total = len(HARNESS_TENANTS) * len(CASES)
    report("─" * 60)
    report(f"Live semantic run complete. {total} checks executed, {errors} failures.")
    report("─" * 60)
    return EXIT_FAILURE if errors else EXIT_SUCCESS


if __name__ == "__main__":
    raise SystemExit(run_cases())
