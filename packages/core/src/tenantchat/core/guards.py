"""The deterministic tool-permission guard (`RAG-007`).

The model is offered only its routed agent's tools and every tool call is
re-checked when it arrives — but an allowlist alone is not a policy check. This
guard is the third, content-independent line: the tool name, the agent's
declared set, and the tenant's server-owned policy, with nothing from the
visitor, the evidence, or the model's own text in the inputs. It is a pure
function, so the graph can apply it to every call and a test can prove an
injected document cannot widen what may run.

The tool vocabulary lives in the orchestration layer; the guard recognizes the
policy-relevant subset by name so the rule stays in the domain where
``TenantPolicy`` lives.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum

from tenantchat.core.tenant import TenantPolicy

# The tools whose execution the tenant's policy gates, independent of the
# agent's allowlist. A booking-disabled tenant must refuse these even when the
# model calls them.
BOOKING_TOOLS = frozenset({"get_availability", "book_appointment"})
LEAD_TOOL = "create_lead"


class ToolRefusal(StrEnum):
    """The bounded reason a tool call may not execute.

    The value is the safe code published back to the model and to the turn
    record; it never carries the model's own text. The booking and lead codes
    are the existing domain codes (``BookingNotPermittedError`` etc.) so the
    model's recovery behavior and the tool-payload contract do not change when
    enforcement moves into this guard.
    """

    NOT_ALLOWED = "tool_not_allowed"
    BOOKING_DISABLED = "booking_not_permitted"
    LEAD_CAPTURE_DISABLED = "lead_capture_not_permitted"


@dataclass(frozen=True, slots=True)
class ToolPermissionVerdict:
    """Whether one tool call may run, with the refusal code when it may not."""

    permitted: bool
    refusal: ToolRefusal | None

    @property
    def refusal_code(self) -> str | None:
        """The publishable refusal code, or ``None`` when permitted."""
        return None if self.refusal is None else self.refusal.value


def tool_permission(
    tool: str,
    *,
    allowed_tools: Collection[str],
    policy: TenantPolicy,
) -> ToolPermissionVerdict:
    """Decide whether one named tool may run for this tenant and agent.

    The checks are ordered by cost of the mistake: a policy-disabled effect
    never runs even when the agent allows it, and a tool outside the agent's
    allowlist never runs. A name nobody declared falls out of the allowlist
    check; there is nothing else to distinguish it by.

    Args:
        tool: The tool name the model wrote.
        allowed_tools: The routed agent's declared tool names.
        policy: The tenant's server-owned policy.

    Returns:
        A verdict; the caller must not execute the call when
        ``verdict.permitted`` is false.
    """
    if tool in BOOKING_TOOLS and not policy.booking_enabled:
        return ToolPermissionVerdict(False, ToolRefusal.BOOKING_DISABLED)
    if tool == LEAD_TOOL and not policy.lead_capture_enabled:
        return ToolPermissionVerdict(False, ToolRefusal.LEAD_CAPTURE_DISABLED)
    if tool not in allowed_tools:
        return ToolPermissionVerdict(False, ToolRefusal.NOT_ALLOWED)
    return ToolPermissionVerdict(True, None)
