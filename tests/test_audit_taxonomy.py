"""The audit action taxonomy is one list, enforced at both ends.

`BUG-018`: the Clearview trail carried `handoff.accepted`, `handoff.resolved`,
`knowledge.version_published`, and `knowledge.version_approved` while the
console's Action dropdown offered none of them, so an operator working an
incident could not filter for the events they were looking at. Keeping the two
lists correct by hand is what failed; these tests are what replaces that.
"""

from __future__ import annotations

import re
from pathlib import Path

from tenantchat.api.store import AUDIT_ACTIONS

_ROOT = Path(__file__).resolve().parents[1]
_ROUTERS = _ROOT / "services" / "api" / "src" / "tenantchat" / "api"
_CONSOLE = _ROOT / "frontend" / "src" / "admin" / "accessTypes.ts"

# Actions reach the audit store either as `action="..."` or positionally
# through a helper such as `_audit_version(..., "knowledge.version_approved",
# ...)`. Both spellings are dotted or underscored lowercase literals.
_EMITTED = re.compile(r'action="([a-z_]+(?:\.[a-z_]+)?)"')
_HELPER_ARG = re.compile(
    r'^\s+"((?:knowledge|handoff|privacy|review|trace)\.[a-z_]+)",\s*$', re.MULTILINE
)


def _emitted_actions() -> set[str]:
    found: set[str] = set()
    for source in _ROUTERS.rglob("*.py"):
        text = source.read_text()
        found.update(_EMITTED.findall(text))
        found.update(match.group(1) for match in _HELPER_ARG.finditer(text) if match)
    return found


def _console_actions() -> set[str]:
    block = re.search(
        r"export const AUDIT_ACTIONS = \[(.*?)\] as const;",
        _CONSOLE.read_text(),
        re.DOTALL,
    )
    assert block is not None, "the console no longer exports an AUDIT_ACTIONS array"
    return set(re.findall(r'"([^"]+)"', block.group(1)))


def test_every_emitted_action_is_in_the_taxonomy() -> None:
    """A new audit action must join the taxonomy, not appear only in a router."""
    undeclared = _emitted_actions() - set(AUDIT_ACTIONS)
    assert not undeclared, (
        f"these actions are emitted but missing from AUDIT_ACTIONS: {sorted(undeclared)}. "
        "Add them to services/api/src/tenantchat/api/store.py and to the console."
    )


def test_the_console_filter_offers_the_whole_taxonomy() -> None:
    """The reproduction of BUG-018: a filterable action the dropdown omits."""
    missing = set(AUDIT_ACTIONS) - _console_actions()
    assert not missing, (
        f"the admin Action filter omits {sorted(missing)}. An operator cannot filter for an "
        "event type that the trail records."
    )


def test_the_console_filter_invents_no_action() -> None:
    """A dropdown entry nothing emits returns an always-empty result set."""
    invented = _console_actions() - set(AUDIT_ACTIONS)
    assert not invented, f"the admin Action filter offers unknown actions: {sorted(invented)}"
