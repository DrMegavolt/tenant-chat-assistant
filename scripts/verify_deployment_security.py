#!/usr/bin/env python3
"""Static SEC-005 gate for tracked Kubernetes deployment inputs.

The manifests are plain YAML rather than templates, so rendering means joining
the deployable documents and removing Secret documents before scanning.  Secret
source documents receive a separate literal-credential scan.  The script never
contacts Kubernetes and never reads `.env`, `.local`, or live Secret objects.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
K8S_DIR = ROOT / "k8s"

PRIVATE_ENDPOINT = re.compile(
    r"https?://(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|"
    r"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})(?::\d+)?",
    re.IGNORECASE,
)
CREDENTIAL_DATABASE_URL = re.compile(r"postgres(?:ql)?://[^\s/:]+:[^\s@/]+@", re.IGNORECASE)
SECRET_DATA_KEY = re.compile(
    r"^\s{2,}(?:password|passwd|apiKey|token|secret|databaseUrl):\s*\S+",
    re.IGNORECASE | re.MULTILINE,
)


class VerificationError(RuntimeError):
    """One or more deployment security invariants failed."""


def is_sensitive_key(name: str) -> bool:
    """Recognize shell-style and camelCase credential-bearing names."""
    normalized = re.sub(r"[^a-z0-9]", "", name.lower())
    return normalized.endswith(("password", "passwd", "apikey", "token", "secret")) or (
        "database" in normalized and normalized.endswith("url")
    )


def is_obvious_placeholder(value: str) -> bool:
    """Allow empty local values and unmistakably inert example values."""
    return not value or "REPLACE_WITH_" in value or "replace-with-" in value or ".invalid" in value


def yaml_documents(text: str) -> list[str]:
    """Split this repository's multi-document YAML without parsing values."""
    return [document.strip() for document in re.split(r"^---\s*$", text, flags=re.MULTILINE)]


def resource_identity(document: str) -> tuple[str, str]:
    kind_match = re.search(r"^kind:\s*([^\s#]+)", document, re.MULTILINE)
    name_match = re.search(
        r"^metadata:\s*\n(?:^[ \t]+.*\n)*?^[ \t]+name:\s*([^\s#]+)",
        document,
        re.MULTILINE,
    )
    return (
        kind_match.group(1) if kind_match else "",
        name_match.group(1) if name_match else "",
    )


def deployment_documents() -> list[tuple[Path, str]]:
    documents: list[tuple[Path, str]] = []
    for path in sorted(K8S_DIR.glob("*.yaml")):
        for document in yaml_documents(path.read_text(encoding="utf-8")):
            if document:
                documents.append((path, document))
    return documents


def non_secret_render(documents: list[tuple[Path, str]]) -> str:
    """Return deployable non-Secret YAML; safe to persist for diagnostics."""
    return "\n---\n".join(
        document for _, document in documents if resource_identity(document)[0] != "Secret"
    )


def _env_block(document: str, variable: str) -> str:
    lines = document.splitlines()
    target = re.compile(rf"^(?P<indent>\s*)- name:\s*{re.escape(variable)}\s*$")
    for index, line in enumerate(lines):
        match = target.match(line)
        if match is None:
            continue
        indent = match.group("indent")
        block: list[str] = []
        for following in lines[index + 1 :]:
            if following.startswith(f"{indent}- name:"):
                break
            if following and len(following) - len(following.lstrip()) <= len(indent):
                break
            block.append(following)
        return "\n".join(block)
    return ""


def _document_for(
    documents: list[tuple[Path, str]], kind: str, name: str
) -> tuple[Path, str] | None:
    return next(
        (
            (path, document)
            for path, document in documents
            if resource_identity(document) == (kind, name)
        ),
        None,
    )


def _require_env_ref(
    errors: list[str],
    document: str,
    workload: str,
    variable: str,
    ref_type: str,
    resource_name: str,
    key: str,
) -> None:
    block = _env_block(document, variable)
    required = (ref_type, f"name: {resource_name}", f"key: {key}")
    if not block or not all(part in block for part in required):
        errors.append(f"{workload}: {variable} must use {ref_type} {resource_name}:{key}")
    if re.search(r"^\s*value:\s*", block, re.MULTILINE):
        errors.append(f"{workload}: {variable} must not contain a literal value")


def _scan_source_documents(errors: list[str], documents: list[tuple[Path, str]]) -> None:
    for path, document in documents:
        kind, name = resource_identity(document)
        label = f"{path.relative_to(ROOT)}:{kind}/{name}"
        private_endpoint = PRIVATE_ENDPOINT.search(document)
        if private_endpoint:
            errors.append(f"{label}: contains a private network endpoint")
        database_url = CREDENTIAL_DATABASE_URL.search(document)
        if database_url:
            errors.append(f"{label}: contains a database URL with literal credentials")

        if kind == "Secret" and SECRET_DATA_KEY.search(document):
            errors.append(f"{label}: contains a literal credential in a Secret document")

        lines = document.splitlines()
        for line in lines:
            env_match = re.match(r"^\s*- name:\s*([A-Z0-9_]+)\s*$", line)
            if not env_match or not is_sensitive_key(env_match.group(1)):
                continue
            block = _env_block(document, env_match.group(1))
            if re.search(r"^\s+value:\s*", block, re.MULTILINE):
                errors.append(
                    f"{label}: sensitive environment variable {env_match.group(1)} has a literal"
                )


def _check_workload_refs(errors: list[str], documents: list[tuple[Path, str]]) -> None:
    workload_documents: dict[str, str] = {}
    for workload in (
        "chat-backend",
        "financing-agent",
        "ingestion-service",
        "embedding-service",
    ):
        found = _document_for(documents, "Deployment", workload)
        if found is None:
            errors.append(f"Deployment/{workload}: missing from deployment input")
            continue
        _, document = found
        workload_documents[workload] = document
        app_env = _env_block(document, "APP_ENV")
        if not re.search(r"^\s*value:\s*production\s*$", app_env, re.MULTILINE):
            errors.append(f"Deployment/{workload}: APP_ENV must select fail-closed production mode")

    chat = workload_documents.get("chat-backend")
    if chat is not None:
        _require_env_ref(
            errors,
            chat,
            "chat-backend",
            "DATABASE_URL",
            "secretKeyRef",
            "postgres-credentials",
            "databaseUrl",
        )

    for workload in ("financing-agent", "ingestion-service"):
        dependency_document = workload_documents.get(workload)
        if dependency_document is None:
            continue
        _require_env_ref(
            errors,
            dependency_document,
            workload,
            "ES_USERNAME",
            "secretKeyRef",
            "elastic-credentials",
            "username",
        )
        _require_env_ref(
            errors,
            dependency_document,
            workload,
            "ES_PASSWORD",
            "secretKeyRef",
            "elastic-credentials",
            "password",
        )

    for workload in ("chat-backend", "financing-agent"):
        llm_document = workload_documents.get(workload)
        if llm_document is None:
            continue
        _require_env_ref(
            errors,
            llm_document,
            workload,
            "LLM_BASE_URL",
            "configMapKeyRef",
            "llm-runtime",
            "baseUrl",
        )
        _require_env_ref(
            errors,
            llm_document,
            workload,
            "LLM_MODEL",
            "configMapKeyRef",
            "llm-runtime",
            "model",
        )
        _require_env_ref(
            errors,
            llm_document,
            workload,
            "LLM_API_KEY",
            "secretKeyRef",
            "llm-provider-credentials",
            "apiKey",
        )
        _require_env_ref(
            errors,
            llm_document,
            workload,
            "LLM_TIMEOUT_SECONDS",
            "configMapKeyRef",
            "llm-runtime",
            "timeoutSeconds",
        )

    internal_refs = {
        "chat-backend": {
            "CHAT_TO_FINANCING_TOKEN": "chat-to-financing-credentials",
        },
        "financing-agent": {
            "CHAT_TO_FINANCING_TOKEN": "chat-to-financing-credentials",
            "FINANCING_TO_EMBEDDING_TOKEN": "financing-to-embedding-credentials",
        },
        "ingestion-service": {
            "SEED_TO_INGESTION_TOKEN": "seed-to-ingestion-credentials",
            "INGESTION_TO_EMBEDDING_TOKEN": "ingestion-to-embedding-credentials",
        },
        "embedding-service": {
            "INGESTION_TO_EMBEDDING_TOKEN": "ingestion-to-embedding-credentials",
            "FINANCING_TO_EMBEDDING_TOKEN": "financing-to-embedding-credentials",
        },
    }
    for workload, references in internal_refs.items():
        internal_document = workload_documents.get(workload)
        if internal_document is None:
            continue
        for variable, secret_name in references.items():
            _require_env_ref(
                errors,
                internal_document,
                workload,
                variable,
                "secretKeyRef",
                secret_name,
                "token",
            )

    seed = _document_for(documents, "Job", "seed-financing-docs")
    if seed is None:
        errors.append("Job/seed-financing-docs: missing from deployment input")
    else:
        _require_env_ref(
            errors,
            seed[1],
            "seed-financing-docs",
            "SEED_TO_INGESTION_TOKEN",
            "secretKeyRef",
            "seed-to-ingestion-credentials",
            "token",
        )


def _check_examples(errors: list[str]) -> None:
    examples = [ROOT / ".env.example", *sorted((K8S_DIR / "examples").glob("*.env.example"))]
    if not examples:
        errors.append("k8s/examples: no placeholder configuration was found")
        return
    for path in examples:
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if CREDENTIAL_DATABASE_URL.search(value) and not is_obvious_placeholder(value):
                errors.append(
                    f"{path.relative_to(ROOT)}: {key} contains a database URL with credentials"
                )
            if is_sensitive_key(key) and not is_obvious_placeholder(value):
                errors.append(f"{path.relative_to(ROOT)}: {key} is not an obvious placeholder")
        if "llm-runtime" in path.name and ".invalid" not in text:
            errors.append(f"{path.relative_to(ROOT)}: example endpoint must use .invalid")


def _check_client_authentication(errors: list[str]) -> None:
    for relative_path in ("server.py", "services/financing-agent/app.py"):
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        if "headers=openai_request_headers(LLM_API_KEY)" not in text:
            errors.append(f"{relative_path}: OpenAI-compatible request is not authenticated")

    internal_markers = {
        "server.py": "internal_bearer_headers(FINANCING_AGENT_TOKEN)",
        "services/ingestion/app.py": "internal_bearer_headers(EMBEDDING_TOKEN)",
        "services/financing-agent/app.py": "internal_bearer_headers(EMBEDDING_TOKEN)",
        "services/embedding/app.py": "Depends(require_embedding_caller)",
    }
    for relative_path, marker in internal_markers.items():
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        if marker not in text:
            errors.append(f"{relative_path}: internal request authentication is missing")


def verify() -> tuple[int, int]:
    documents = deployment_documents()
    rendered = non_secret_render(documents)
    errors: list[str] = []

    _scan_source_documents(errors, documents)
    _check_workload_refs(errors, documents)
    _check_examples(errors)
    _check_client_authentication(errors)

    if PRIVATE_ENDPOINT.search(rendered) or CREDENTIAL_DATABASE_URL.search(rendered):
        errors.append("rendered non-Secret output contains a sensitive endpoint or credential")
    if re.search(r"^kind:\s*Secret\s*$", rendered, re.MULTILINE):
        errors.append("rendered non-Secret output unexpectedly contains a Secret")

    if errors:
        raise VerificationError("\n".join(f"- {error}" for error in errors))
    return len(documents), rendered.count("\n---\n") + bool(rendered)


def main() -> int:
    try:
        source_count, rendered_count = verify()
    except VerificationError as error:
        sys.stderr.write(f"deployment security verification failed:\n{error}\n")
        return 1
    sys.stdout.write(
        "deployment security verification passed "
        f"({source_count} source documents; {rendered_count} rendered non-Secret documents)\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
