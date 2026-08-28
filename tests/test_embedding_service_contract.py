"""R-67: the embedding service's load and readiness contract.

``services/embedding/app.py`` cannot be imported hermetically — its model
runtime is a multi-gigabyte dependency the gate deliberately does not install —
so these specifications read the module's syntax tree the way the audit
taxonomy test reads the console source. Three properties are load-bearing: a
readiness probe must never trigger the multi-minute model load (an orchestrator
gates traffic on readiness, so a probe-triggered load would never be reached
and the pod would report "loading" forever), concurrent first callers must
serialize into one load, and the process must start the load itself at startup.
"""

from __future__ import annotations

import ast
from pathlib import Path

_APP = Path(__file__).resolve().parents[1] / "services" / "embedding" / "app.py"


def _module() -> ast.Module:
    return ast.parse(_APP.read_text())


def _function(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{_APP.name} defines no function {name!r}")


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    return [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and (
            (isinstance(child.func, ast.Name) and child.func.id == name)
            or (isinstance(child.func, ast.Attribute) and child.func.attr == name)
        )
    ]


def _has_503_keyword(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Call)
        and any(
            keyword.arg == "status_code"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value == 503
            for keyword in child.keywords
        )
        for child in ast.walk(node)
    )


def test_the_readiness_probe_never_triggers_the_model_load() -> None:
    tree = _module()
    ready = _function(tree, "ready")

    assert not _calls(ready, "get_model"), (
        "/ready must report the current state, not start the load: a readiness-"
        "gated orchestrator sends no traffic until /ready passes, so a probe "
        "that loads the model deadlocks the deployment in 'loading'."
    )
    assert _has_503_keyword(
        ready
    ), "while the model is not resident /ready must answer 503, not 200"


def test_concurrent_first_callers_serialize_behind_a_lock() -> None:
    tree = _module()
    get_model = _function(tree, "get_model")

    enters_lock = any(
        isinstance(child, ast.With)
        and any(
            isinstance(item.context_expr, ast.Name) and item.context_expr.id == "_MODEL_LOCK"
            for item in child.items
        )
        for child in ast.walk(get_model)
    )
    assert enters_lock, (
        "get_model must hold _MODEL_LOCK across the check-then-load: the "
        "endpoints run in a threadpool, so two concurrent first callers are "
        "the normal case, and each would start its own multi-minute download."
    )


def test_startup_begins_loading_the_model() -> None:
    """The complement to the probe fix: nothing else would ever start the
    load on a readiness-gated deployment, so the lifespan must."""
    tree = _module()
    loaders = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _calls(node, "Thread")
    ]
    assert loaders, "no startup path starts the model load in a background thread"

    lifespan_targeted = False
    for loader in loaders:
        for call in _calls(loader, "Thread"):
            if any(
                isinstance(keyword.value, ast.Name) and keyword.value.id == "get_model"
                for keyword in call.keywords
                if keyword.arg == "target"
            ):
                lifespan_targeted = True
    assert lifespan_targeted, (
        "the background thread must load the model (target=get_model), not " "merely exist"
    )

    app_constructor = next(
        (node for node in tree.body if isinstance(node, ast.Assign) and _targets_app(node)),
        None,
    )
    assert app_constructor is not None, "the module defines no FastAPI app"
    lifespan_kw = [
        keyword
        for call in ast.walk(app_constructor)
        if isinstance(call, ast.Call)
        for keyword in call.keywords
        if keyword.arg == "lifespan"
    ]
    assert lifespan_kw, "the FastAPI app must register the lifespan that starts the load"


def _targets_app(node: ast.Assign) -> bool:
    return any(isinstance(target, ast.Name) and target.id == "app" for target in node.targets)
