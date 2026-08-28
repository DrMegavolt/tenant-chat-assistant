#!/usr/bin/env python3
"""Seed governed knowledge for both demo tenants through the real lifecycle.

upload -> approve -> publish -> ingest -> poll-for-completion

Idempotent: source registrations, uploads, and ingestion jobs deduplicate, so
re-running against an already-seeded cluster is a no-op.

Environment:
    API_BASE_URL          admin API root (default http://chat-admin:8004)
    ADMIN_GATEWAY_TOKEN   shared gateway-to-API token for auth
    ADMIN_CSRF_SECRET     CSRF signing secret
    SEED_API_TIMEOUT      per-request HTTP timeout in seconds (default 30)
    SEED_POLL_INTERVAL    seconds between job-status polls (default 2)
    SEED_POLL_ATTEMPTS    max poll cycles before giving up (default 60)
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import time
import uuid
from pathlib import Path
from typing import cast
from urllib.parse import urljoin, urlsplit

_TENANTS = (
    ("apex", "docs/apex/financing/financing-options.md", "Apex financing options"),
    ("clearview", "docs/clearview/financing/financing-options.md", "Clearview financing options"),
)

_DRAFT_STATE = "draft"
_JOB_SUCCEEDED = "succeeded"
_JOB_TERMINAL_FAILURES = ("dead_lettered", "cancelled")
_DOMAIN = "financing"
_SOURCE_KIND = "upload"
_SOURCE_DISPLAY = "Financing options"
_OPERATOR_SUBJECT = "seed-operator"
_OPERATOR_EMAIL = "seed@operator.internal"
_OPERATOR_ROLE = "platform_admin"

_GATEWAY_TOKEN_HEADER = "X-TenantChat-Gateway-Token"  # noqa: S105
_SUBJECT_HEADER = "X-Auth-Subject"
_EMAIL_HEADER = "X-Auth-Email"
_ROLE_HEADER = "X-Auth-Role"
_CSRF_HEADER = "X-CSRF-Token"


def report(line: str) -> None:
    print(line, flush=True)  # noqa: T201


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _require(name: str) -> str:
    value = _env(name)
    if not value:
        report(f"FATAL: {name} is required")
        raise SystemExit(2)
    return value


BASE_URL = _env("API_BASE_URL", "http://chat-admin:8004")
TIMEOUT = int(_env("SEED_API_TIMEOUT", "30"))
POLL_INTERVAL = float(_env("SEED_POLL_INTERVAL", "2"))
POLL_ATTEMPTS = int(_env("SEED_POLL_ATTEMPTS", "60"))


def _admin_headers(*, csrf: str = "") -> dict[str, str]:
    headers = {
        _GATEWAY_TOKEN_HEADER: _require("ADMIN_GATEWAY_TOKEN"),
        _SUBJECT_HEADER: _OPERATOR_SUBJECT,
        _EMAIL_HEADER: _OPERATOR_EMAIL,
        _ROLE_HEADER: _OPERATOR_ROLE,
    }
    if csrf:
        headers[_CSRF_HEADER] = csrf
    return headers


def _csrf_token() -> str:
    secret = _require("ADMIN_CSRF_SECRET")
    return hmac.new(secret.encode(), _OPERATOR_SUBJECT.encode(), hashlib.sha256).hexdigest()


def _connect(url: str) -> tuple[http.client.HTTPConnection, str]:
    """A connection and its request path for an absolute http(s) URL.

    The scheme selects the connection class, and a missing port falls back to
    each class's own default (80 / 443); a plaintext connection to an https
    endpoint would otherwise fail confusingly downstream, not here. Same
    pattern as ``scripts/harness_live.py``.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError(f"API URL must be an absolute http(s) URL: {url!r}")
    connection_type = (
        http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    )
    uri = parsed.path or "/"
    if parsed.query:
        uri = f"{uri}?{parsed.query}"
    return connection_type(parsed.hostname, parsed.port, timeout=TIMEOUT), uri


def _request(
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    content_type: str = "application/json",
) -> tuple[int, dict[str, object]]:
    conn: http.client.HTTPConnection | None = None
    try:
        conn, uri = _connect(urljoin(BASE_URL, path))
        hdrs: dict[str, str] = {
            "Content-Type": content_type,
            "Accept": "application/json",
        }
        if headers:
            hdrs.update(headers)
        conn.request(method, uri, body=body, headers=hdrs)
        response = conn.getresponse()
        raw = response.read()
        try:
            data: dict[str, object] = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {"_body": raw.decode(errors="replace")}
        return response.status, data
    except Exception as exc:
        report(f"HTTP {method} {path} failed: {exc}")
        raise
    finally:
        if conn is not None:
            conn.close()


def _get(path: str, *, headers: dict[str, str] | None = None) -> dict[str, object]:
    status, body = _request("GET", path, headers=headers)
    if status >= 300:
        report(f"GET {path} returned {status}: {body}")
        raise RuntimeError(f"GET {path} failed with status {status}")
    return body


def _post(
    path: str,
    *,
    json_body: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, object]:
    raw = json.dumps(json_body).encode() if json_body else None
    status, body = _request("POST", path, body=raw, headers=headers)
    if status >= 300:
        report(f"POST {path} returned {status}: {body}")
        raise RuntimeError(f"POST {path} failed with status {status}")
    return body


def _multipart_post(
    path: str,
    fields: dict[str, str],
    file_content: bytes,
    filename: str,
    media_type: str,
    headers: dict[str, str],
) -> dict[str, object]:
    boundary = f"----seed-{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(f"{value}\r\n".encode())
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode())
    parts.append(f"Content-Type: {media_type}\r\n\r\n".encode())
    parts.append(file_content)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body_bytes = b"".join(parts)
    content_type = f"multipart/form-data; boundary={boundary}"
    status, body = _request(
        "POST", path, body=body_bytes, headers=headers, content_type=content_type
    )
    if status >= 300:
        report(f"POST {path} returned {status}: {body}")
        raise RuntimeError(f"POST {path} failed with status {status}")
    return body


def _register_source(tenant_id: str, csrf: str) -> uuid.UUID:
    body = _post(
        "/api/admin/knowledge/sources",
        json_body={
            "tenant_id": tenant_id,
            "domain": _DOMAIN,
            "kind": _SOURCE_KIND,
            "display_name": _SOURCE_DISPLAY,
        },
        headers=_admin_headers(csrf=csrf),
    )
    source_id = uuid.UUID(assert_str(body, "source_id"))
    report(f"source {source_id} registered for {tenant_id}")
    return source_id


def _upload_document(
    tenant_id: str, source_id: uuid.UUID, filepath: str, title: str, csrf: str
) -> tuple[str, str, str]:
    content = _read_document(filepath)
    body = _multipart_post(
        "/api/admin/knowledge/uploads",
        fields={
            "tenant_id": tenant_id,
            "source_id": str(source_id),
            "external_key": filepath,
            "title": title,
        },
        file_content=content,
        filename=Path(filepath).name,
        media_type="text/markdown",
        headers=_admin_headers(csrf=csrf),
    )
    version_id = assert_str(body, "version_id")
    document_id = assert_str(body, "document_id")
    state = assert_str(body, "state")
    report(f"uploaded {filepath} -> version {version_id} (state={state})")
    return version_id, document_id, state


def _approve(tenant_id: str, version_id: str, csrf: str) -> None:
    body = _post(
        f"/api/admin/knowledge/versions/{version_id}/approve",
        json_body={"tenant_id": tenant_id},
        headers=_admin_headers(csrf=csrf),
    )
    state = cast(dict[str, object], body.get("version", {})).get("state", "unknown")
    report(f"approved version {version_id} (state={state})")


def _publish(tenant_id: str, version_id: str, csrf: str) -> str | None:
    body = _post(
        f"/api/admin/knowledge/versions/{version_id}/publish",
        json_body={"tenant_id": tenant_id},
        headers=_admin_headers(csrf=csrf),
    )
    state = cast(dict[str, object], body.get("version", {})).get("state", "unknown")
    job = body.get("job")
    job_id = job.get("job_id") if isinstance(job, dict) else None
    report(f"published version {version_id} (state={state}, job={job_id})")
    return job_id


def _wait_for_job(tenant_id: str, job_id: str | None) -> None:
    if job_id is None:
        report("no ingestion job to wait for (already indexed)")
        return
    headers = _admin_headers()
    for attempt in range(1, POLL_ATTEMPTS + 1):
        body = _get(f"/api/admin/jobs/{job_id}?tenant_id={tenant_id}", headers=headers)
        job = body.get("job")
        status = job.get("status", "unknown") if isinstance(job, dict) else "unknown"
        if status == _JOB_SUCCEEDED:
            report(f"job {job_id} succeeded after {attempt} poll(s)")
            return
        if status in _JOB_TERMINAL_FAILURES:
            report(f"FATAL: job {job_id} entered status {status}: {body}")
            raise RuntimeError(f"ingestion job {job_id} failed: {status}")
        if attempt % 10 == 0:
            report(f"waiting for job {job_id} (status={status}, attempt={attempt})...")
        time.sleep(POLL_INTERVAL)
    report(f"FATAL: job {job_id} did not complete within {POLL_ATTEMPTS} attempts")
    raise RuntimeError(f"job {job_id} timed out")


def _verify_indexed(tenant_id: str, version_id: str) -> None:
    headers = _admin_headers()
    body = _get(
        f"/api/admin/knowledge?tenant_id={tenant_id}&limit=200",
        headers=headers,
    )
    sources = body.get("sources", [])
    if not isinstance(sources, list):
        return
    for source in sources:
        for doc in source.get("documents", []):
            for ver in doc.get("versions", []):
                if ver.get("version_id") == version_id:
                    idx = ver.get("indexing_state", "pending")
                    report(f"version {version_id} indexing_state={idx} for {tenant_id}")
                    if idx != "indexed":
                        report(f"WARNING: version {version_id} not yet indexed (state={idx})")
                    return
    report(f"WARNING: version {version_id} not found in knowledge tree for {tenant_id}")


def assert_str(body: dict[str, object], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"expected string '{key}' in response, got {value!r}")
    return value


def _read_document(filepath: str) -> bytes:
    path = Path(filepath)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        report(f"FATAL: document not found: {filepath}")
        raise


def seed_tenant(tenant_id: str, filepath: str, title: str) -> None:
    report(f"\n--- seeding {tenant_id} ---")
    csrf = _csrf_token()

    source_id = _register_source(tenant_id, csrf)
    version_id, document_id, state = _upload_document(tenant_id, source_id, filepath, title, csrf)
    # Upload deduplicates on external_key, so a re-run gets back the version an
    # earlier run already advanced. Approval admits draft alone and answers 409
    # for anything further along; publish accepts an already-published version
    # and reindexes it, which is what makes the whole seed re-runnable.
    if state == _DRAFT_STATE:
        _approve(tenant_id, version_id, csrf)
    else:
        report(f"version {version_id} is already {state}; skipping approval")
    job_id = _publish(tenant_id, version_id, csrf)
    _wait_for_job(tenant_id, job_id)
    _verify_indexed(tenant_id, version_id)
    report(f"done seeding {tenant_id}: document={document_id} version={version_id}")


def main() -> int:
    report(f"seed targeting {BASE_URL}")
    for tenant_id, filepath, title in _TENANTS:
        try:
            seed_tenant(tenant_id, filepath, title)
        except RuntimeError as exc:
            report(f"FAILED seeding {tenant_id}: {exc}")
            return 1
    report("\nseed complete for all tenants")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
