"""Executable enforcement of the ADR-0001 layering rule.

ADR-0001 accepts an agent framework at the edge while requiring that
authentication, authorization, validation, transactions, and idempotency stay in
deterministic domain services. Prose alone does not hold that line: the failure
mode is a well-meaning framework import into a domain module, one file at a time,
until the "framework-independent" layer cannot be tested without the framework.

**Scope.** ``packages/core`` in full, plus ``services/api`` minus its composition
root. This is a *dependency-direction* policy, not a repository-wide import ban —
LangGraph is expected and correct in orchestration, checkpoint adapters, and the
composition root, and FastAPI is expected throughout ``services/api``. See
ADR-0001's layer table for what each layer may reference.

The two scans ban different things on purpose. The domain layer may not reach
for a framework of any kind. The API layer is *made* of frameworks; what it may
not do is let the agent framework into the application contracts, request and
response schemas, and repository adapters that everything else is written
against.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE_ROOT = REPO_ROOT / "packages" / "core"
CORE_SOURCE = CORE_ROOT / "src"
API_SOURCE = REPO_ROOT / "services" / "api" / "src"

# Agent frameworks, banned outside orchestration, the checkpoint adapter, and the
# composition root. Model SDKs are here too: `AI-001` puts provider choice behind
# a port, and a provider client imported into a schema or a repository is that
# port being routed around.
AGENT_FRAMEWORK_IMPORTS: dict[str, str] = {
    "langchain": "agent framework",
    "langchain_core": "agent framework",
    "langgraph": "agent framework",
    "llama_index": "agent framework",
    "tenantchat.orchestration": "agent runtime",
    "openai": "model SDK",
    "anthropic": "model SDK",
}

# The composition root, and the only place in `services/api` that may name the
# agent runtime. Adding an entry here is a design decision about the layer
# boundary, which is why it is a list of files rather than a directory pattern.
# `replay.py` earns its entry because a safe replay composes a stored turn
# record with the current model and the current component versions — the same
# wiring the root owns — and `FEAT-015` documents it as part of that boundary.
COMPOSITION_ROOT: frozenset[str] = frozenset(
    {
        "tenantchat/api/app.py",
        "tenantchat/api/agent.py",
        "tenantchat/api/replay.py",
    }
)

# Third-party packages the domain layer may use, as {distribution: import name}.
#
# The bar is high: a candidate must be deterministic, perform no I/O, pull no
# framework or transport, and encode a domain concern a hand-rolled version would
# get measurably wrong. `phonenumbers` is the standing example — the moment this
# product serves numbers outside the North American plan, a hand-written regex
# becomes a liability and that library earns its place. Zero entries is therefore
# a property of core today, not the rule. The rule is the ban list below.
APPROVED_DOMAIN_LIBRARIES: dict[str, str] = {}

# Categories that may never enter the domain layer, whatever the justification.
BANNED_IMPORTS: dict[str, str] = {
    # Web frameworks: domain rules must be callable from a worker or a test.
    "fastapi": "web framework",
    "starlette": "web framework",
    "flask": "web framework",
    # Persistence: the domain defines ports; adapters implement them.
    "sqlalchemy": "ORM",
    "psycopg": "database driver",
    "alembic": "migration tool",
    "elasticsearch": "search client",
    # Agent frameworks: allowed in orchestration and checkpoint adapters, never here.
    "langchain": "agent framework",
    "langchain_core": "agent framework",
    "langgraph": "agent framework",
    "llama_index": "agent framework",
    # Model vendors: provider choice must not reach domain types (AI-001).
    "openai": "model SDK",
    "anthropic": "model SDK",
    "google": "model SDK",
    "sentence_transformers": "model SDK",
    # Transports: a domain rule that performs I/O is not a domain rule.
    "requests": "HTTP client",
    "httpx": "HTTP client",
    "aiohttp": "HTTP client",
    # Validation framework: belongs at the API edge, shaping requests and responses.
    "pydantic": "validation framework",
}

_REQUIREMENT_NAME = re.compile(r"^[A-Za-z0-9._-]+")


def core_modules() -> list[Path]:
    return sorted(CORE_SOURCE.rglob("*.py"))


def imported_top_level_names(module_path: Path) -> Iterator[tuple[str, int]]:
    """Yield every top-level package name imported by a module, with its line."""
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0], node.lineno
        # `level > 0` is a relative import, which ruff's TID rules already ban.
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module.split(".")[0], node.lineno


def imported_modules(module_path: Path) -> Iterator[tuple[str, int]]:
    """Yield every module a file imports by its full dotted name, with its line.

    Distinct from :func:`imported_top_level_names` because the interesting rule
    for ``services/api`` is about a *submodule*: ``tenantchat`` is the repository
    and ``tenantchat.orchestration`` is the agent runtime.
    """
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module, node.lineno


def api_modules() -> list[Path]:
    """Every ``services/api`` module the layer policy applies to."""
    return sorted(
        path
        for path in API_SOURCE.rglob("*.py")
        if str(path.relative_to(API_SOURCE)) not in COMPOSITION_ROOT
    )


def _banned(name: str) -> str | None:
    for banned, reason in AGENT_FRAMEWORK_IMPORTS.items():
        if name == banned or name.startswith(f"{banned}."):
            return reason
    return None


def test_core_source_tree_is_not_empty() -> None:
    """Guard against the checks below passing vacuously."""
    assert core_modules(), f"no modules found under {CORE_SOURCE}"


def test_core_declares_only_approved_dependencies() -> None:
    """Read the manifest itself, so the guarantee is not just a comment."""
    manifest = tomllib.loads((CORE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = manifest["project"].get("dependencies", [])

    names = {
        match.group(0).lower()
        for requirement in declared
        if (match := _REQUIREMENT_NAME.match(requirement))
    }
    unapproved = sorted(names - set(APPROVED_DOMAIN_LIBRARIES))

    assert not unapproved, (
        f"packages/core declares unapproved dependencies {unapproved}. "
        "If a domain rule needs an external system, define a Protocol port in "
        "tenantchat.core and implement the adapter in the service that owns it. "
        "If it needs a pure domain library, add it to APPROVED_DOMAIN_LIBRARIES "
        "with a note in ADR-0001."
    )


@pytest.mark.parametrize("module_path", core_modules(), ids=lambda path: path.name)
def test_core_module_imports_no_framework(module_path: Path) -> None:
    """No domain module may import a framework, driver, transport, or model SDK."""
    violations = [
        (name, lineno, BANNED_IMPORTS[name])
        for name, lineno in imported_top_level_names(module_path)
        if name in BANNED_IMPORTS
    ]

    assert not violations, "\n".join(
        f"{module_path.relative_to(REPO_ROOT)}:{lineno} imports {name!r} ({reason}). "
        "Define a Protocol port instead and implement it in the adapter layer."
        for name, lineno, reason in violations
    )


@pytest.mark.parametrize("module_path", core_modules(), ids=lambda path: path.name)
def test_core_module_imports_only_stdlib_or_approved(module_path: Path) -> None:
    """Backstop for third-party imports the ban list has not yet heard of.

    The ban list documents *why* specific categories are excluded; this catches a
    novel dependency that belongs to none of them and was never considered.
    """
    allowed = sys.stdlib_module_names | {"tenantchat"} | set(APPROVED_DOMAIN_LIBRARIES.values())
    unexpected = sorted(
        {
            name
            for name, _ in imported_top_level_names(module_path)
            if name not in allowed and not name.startswith("_")
        }
    )

    assert not unexpected, (
        f"{module_path.relative_to(REPO_ROOT)} imports unapproved package(s) {unexpected}. "
        "See APPROVED_DOMAIN_LIBRARIES in this module for the criteria and the "
        "process for adding one."
    )


def test_api_source_tree_is_not_empty() -> None:
    """Guard against the API checks below passing vacuously."""
    assert api_modules(), f"no modules found under {API_SOURCE}"


def test_the_composition_root_is_the_only_exemption_and_it_exists() -> None:
    """A stale exemption silently widens the policy it was written to narrow."""
    missing = sorted(name for name in COMPOSITION_ROOT if not (API_SOURCE / name).is_file())
    assert not missing, (
        f"COMPOSITION_ROOT names files that no longer exist: {missing}. "
        "Remove the entry, or point it at the module that took over the wiring."
    )


@pytest.mark.parametrize("module_path", api_modules(), ids=lambda path: path.name)
def test_api_module_does_not_import_the_agent_framework(module_path: Path) -> None:
    """Application contracts, API schemas, and repository adapters stay framework-free.

    ADR-0001 keeps LangGraph out of these layers so that the runtime can be
    replaced, or run beside a worker or a batch job, without any of them
    changing. The failure mode this prevents is gradual: one graph type in a
    response schema, one checkpoint lookup in a repository, and the boundary is
    gone without anyone having decided to remove it.
    """
    violations = [
        (name, lineno, reason)
        for name, lineno in imported_modules(module_path)
        if (reason := _banned(name)) is not None
    ]

    assert not violations, "\n".join(
        f"{module_path.relative_to(REPO_ROOT)}:{lineno} imports {name!r} ({reason}). "
        "The agent runtime is reachable only from the composition root; take a "
        "`tenantchat.core.ports` Protocol instead."
        for name, lineno, reason in violations
    )


def test_the_composition_root_actually_wires_the_agent_runtime() -> None:
    """The exemption is justified by use, not by convention.

    Without this, ``COMPOSITION_ROOT`` could keep an entry for a module that
    stopped composing anything, leaving a hole in the scan that looks
    deliberate.
    """
    wiring = REPO_ROOT / "services/api/src/tenantchat/api/agent.py"
    imports = {name for name, _ in imported_modules(wiring)}

    assert any(name.startswith("tenantchat.orchestration") for name in imports), (
        f"{wiring.relative_to(REPO_ROOT)} is exempt from the agent-framework scan "
        "but no longer composes the runtime. Drop it from COMPOSITION_ROOT."
    )
