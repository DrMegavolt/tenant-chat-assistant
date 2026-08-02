"""Executable enforcement of the ADR-0001 layering rule.

ADR-0001 accepts an agent framework at the edge while requiring that
authentication, authorization, validation, transactions, and idempotency stay in
deterministic domain services. Prose alone does not hold that line: the failure
mode is a well-meaning framework import into a domain module, one file at a time,
until the "framework-independent" layer cannot be tested without the framework.

**Scope.** ``packages/core`` only. This is a *dependency-direction* policy, not a
repository-wide import ban — LangGraph is expected and correct in orchestration,
checkpoint adapters, and the composition root, and FastAPI is expected in
``services/api``. See ADR-0001's layer table for what each layer may reference.
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
