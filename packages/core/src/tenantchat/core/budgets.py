"""Per-tenant model safety, quotas, and cost controls (`AI-002`).

The resilience envelope in :mod:`tenantchat.core.resilience` bounds how long a
dependency call may take and how fast a client gives up on a dead one. This
module is its sibling for *spend*: a per-tenant budget for model tokens and
business actions, a concurrency cap so one tenant's sessions cannot consume the
whole process, deterministic input/output policy checks for the home-services
domain, and spend alerts that fire once per threshold crossing.

Two properties are structural rather than convention:

- **The accounting is content-free.** The ledger keys on the tenant ID and
  stores integers only — token counts, action counts, in-flight calls. Nothing
  a visitor said or a model answered ever reaches this module, so cost
  attribution to a tenant can never drag user content into the operational
  plane (`ADR-0010`).
- **Enforcement is a guard, not a gatekeeper with a stake.** A check that
  refuses a call returns a bounded :class:`PolicyVerdict` naming a
  :class:`BlockReason`; the graph decides what to say. The ledger never writes
  a domain row, so it can never itself be the thing that "executes a partial
  action".

The ledger is in-memory because `AI-002` ships no migration: like the
``RateLimitStore`` in ``services/api``, the shape is a single bounded service
and the durable, multi-process implementation is a documented follow-up. A
replayed node re-checks and re-records against the same window, which is
guarded the safe direction: an inflated count refuses sooner, never commits
twice.
"""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol

from tenantchat.core.metrics import (
    AlertLevel,
    BlockReason,
    MetricLabelName,
    MetricName,
    MetricsReporter,
)

# The control characters that mark a message as binary rather than text.
# Newline, carriage return, and tab are legitimate transcript characters and
# deliberately excluded; everything else in C0 and DEL has no place in a
# home-services message and indicates a serialization fault or a hostile
# payload, so the assistant refuses rather than forwarding it to a provider.
_BINARY_CONTROL: re.Pattern[str] = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# The rolling window the ``daily_*`` limits are measured over. A window rather
# than a calendar day: counts decay a day after they were spent, so a tenant
# that tripped the budget yesterday can use the service today without a restart,
# and a replayed node's inflated count refuses sooner but never permanently.
_WINDOW_SECONDS: Final = 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class TenantBudget:
    """The spend and content limits one tenant runs under.

    ``None`` on :class:`~tenantchat.core.tenant.TenantPolicy` means "the
    platform default", never "unlimited": a tenant without a record still has a
    budget, so a configuration gap degrades the same way an exhausted one does.

    ``daily_*`` limits are per rolling 24-hour window owned by the ledger, not
    a calendar day — spend older than the window decays, so a tenant that
    tripped the budget yesterday recovers without a restart (which is also what
    makes the graph's deliberate replay of ``record_usage``/``record_action``
    safe: an inflated count refuses sooner but never permanently).
    ``spend_*_threshold_tokens`` fire a one-shot :class:`AlertLevel` when the
    tenant's cumulative tokens cross them. ``max_message_chars`` and
    ``max_output_chars`` are the deterministic input/output policy: the
    assistant refuses content outside them rather than truncating it silently,
    which is the same reporting-over-truncation principle `AI-003` applies to
    prompt assembly.
    """

    daily_token_budget: int = 200_000
    max_concurrent_requests: int = 4
    max_actions_per_turn: int = 8
    max_actions_per_day: int = 100
    max_message_chars: int = 4000
    max_output_chars: int = 4000
    spend_warn_threshold_tokens: int | None = None
    spend_critical_threshold_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.daily_token_budget < 1:
            raise ValueError("daily_token_budget must be positive")
        if self.max_concurrent_requests < 1:
            raise ValueError("max_concurrent_requests must be positive")
        if self.max_actions_per_turn < 1:
            raise ValueError("max_actions_per_turn must be positive")
        if self.max_actions_per_day < 1:
            raise ValueError("max_actions_per_day must be positive")
        if self.max_message_chars < 1 or self.max_output_chars < 1:
            raise ValueError("content length limits must be positive")


# The budget a tenant with no ``TenantBudget`` record runs under. Generous
# enough for a busy demo, small enough that a runaway loop still stops — and,
# crucially, defined once so a reviewer tunes the platform default in one place.
DEFAULT_TENANT_BUDGET: TenantBudget = TenantBudget()


@dataclass(frozen=True, slots=True)
class PolicyVerdict:
    """One budget or content-policy check's outcome.

    ``allowed`` carries the decision; ``reason`` names the block when refused,
    which is the value that reaches the ``POLICY_BLOCKS`` metric. A refusal
    never carries content: the message and the model output stay where the
    graph found them.
    """

    allowed: bool
    reason: BlockReason | None = None

    @property
    def block_reason(self) -> BlockReason:
        """The reason a refusal carries, for the call site that observed one.

        Only ever read on a refused verdict, and a refused verdict is always
        built with a reason — reading this on an allowance is an invariant
        violation worth raising, not a None to paper over.
        """
        if self.reason is None:
            raise AssertionError("an allowed policy verdict has no block reason")
        return self.reason


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    """A tenant's current ledger state, for attribution — and nothing else.

    The whole point of `AI-002` is that usage and cost are attributable to a
    tenant *without* exposing user content, so this is the entire read
    surface: identifiers and counts only. ``alerts_fired`` is the bounded set
    of alert levels already emitted, which is what keeps a snapshot from
    re-arming a spend alert.
    """

    tenant_id: str
    tokens_used: int
    actions_committed: int
    concurrent_calls: int
    alerts_fired: tuple[AlertLevel, ...]


def check_input(message: str, budget: TenantBudget) -> PolicyVerdict:
    """The deterministic input policy: a message a provider must not see.

    The HTTP layer already bounds the request body (`SEC-003`); this is the
    *content* policy a tenant's plan can tighten — a per-message length cap and
    a binary-content refusal. Both are business-domain checks a home-services
    assistant can make without a model.
    """
    if len(message) > budget.max_message_chars:
        return PolicyVerdict(False, BlockReason.INPUT_TOO_LONG)
    if _BINARY_CONTROL.search(message) is not None:
        return PolicyVerdict(False, BlockReason.INPUT_BINARY)
    return PolicyVerdict(True)


def check_output(content: str, budget: TenantBudget) -> PolicyVerdict:
    """The deterministic output policy: over-length model prose is refused.

    Truncation is rejected on the same grounds `AI-003` rejects silent prompt
    cuts: the visitor gets a server-written reply, and the over-long answer is
    an observable block rather than an answer with its tail missing.
    """
    if len(content) > budget.max_output_chars:
        return PolicyVerdict(False, BlockReason.OUTPUT_TOO_LONG)
    return PolicyVerdict(True)


class BudgetLedger(Protocol):
    """Per-tenant accounting the graph enforces against.

    ``budget`` is supplied by the caller from the tenant's policy, never stored
    on the ledger, so a plan change applies to the next check without a reset.
    """

    async def check_token_budget(self, tenant_id: str, budget: TenantBudget) -> PolicyVerdict:
        """Whether the tenant may spend more tokens at all (a pre-flight gate)."""
        ...

    async def enter_call(self, tenant_id: str, budget: TenantBudget) -> PolicyVerdict:
        """Begin a model call: re-check the budget and reserve concurrency.

        Returns a refusal when the token budget is exhausted or the concurrency
        cap is reached; a refusal reserves nothing. ``exit_call`` must be
        called exactly once after an accepted call.
        """
        ...

    async def exit_call(self, tenant_id: str) -> None:
        """Release the concurrency slot :meth:`enter_call` reserved."""
        ...

    async def record_usage(
        self,
        tenant_id: str,
        budget: TenantBudget,
        *,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> tuple[AlertLevel, ...]:
        """Accumulate one completed call's tokens and fire any crossed alerts.

        Returns the alerts that crossed, each of which was also recorded as a
        metric, so the caller can log them under the tenant pseudonym.
        """
        ...

    async def check_action(
        self, tenant_id: str, budget: TenantBudget, *, turn_index: int
    ) -> PolicyVerdict:
        """Whether the tenant may commit another business action now.

        Enforces both the daily action cap and the per-turn cap; ``turn_index``
        is the checkpoint's own counter, so two nodes in one turn share the
        count and a replayed turn re-checks the same window.
        """
        ...

    async def record_action(self, tenant_id: str, *, turn_index: int) -> None:
        """Count one committed business action for attribution."""
        ...

    def snapshot(self, tenant_id: str) -> UsageSnapshot:
        """The tenant's current usage, for attribution and tests."""
        ...


class BudgetEnforcer:
    """One process's per-tenant budget and concurrency ledger.

    Follows the ``RateLimitStore`` precedent: correct within one process,
    wrong once a fleet shares traffic, which is exactly what the durable
    multi-process implementation (a documented follow-up) exists for. All
    mutation happens on one event loop, so a check-then-record pair is atomic
    between awaits.

    The ``daily_*`` limits are measured over a rolling :data:`_WINDOW_SECONDS`
    window: spend events carry a monotonic timestamp and are pruned on read, so
    a tenant's spend decays a day after it was incurred instead of wedging the
    tenant for the process lifetime.

    Args:
        metrics: Optional reporter for spend alerts. When ``None``, no alerts
            are recorded — the ledger still refuses, so a deployment that
            forgets the reporter loses observability, never enforcement.
        clock: Injectable monotonic clock for deterministic tests; defaults to
            :func:`time.monotonic`.
    """

    def __init__(
        self,
        *,
        metrics: MetricsReporter | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._metrics = metrics
        self._clock = clock
        self._token_spend: dict[str, deque[tuple[float, int]]] = {}
        self._action_spend: dict[str, deque[tuple[float, int]]] = {}
        self._concurrent: dict[str, int] = {}
        self._fired: dict[str, set[AlertLevel]] = {}
        # tenant -> (turn_index, actions in that turn). -1 is the "no turn
        # yet" sentinel; a fresh turn resets the count at first contact.
        self._turn_actions: dict[str, tuple[int, int]] = {}

    def _window_sum(self, events: dict[str, deque[tuple[float, int]]], tenant_id: str) -> int:
        """Drop spend older than the window and total what remains."""
        queue = events.get(tenant_id)
        if not queue:
            return 0
        cutoff = self._clock() - _WINDOW_SECONDS
        while queue and queue[0][0] <= cutoff:
            queue.popleft()
        return sum(amount for _, amount in queue)

    def _record(
        self, events: dict[str, deque[tuple[float, int]]], tenant_id: str, amount: int
    ) -> None:
        queue = events.setdefault(tenant_id, deque())
        # The window is rolling, so appends are already time-ordered.
        queue.append((self._clock(), amount))

    async def check_token_budget(self, tenant_id: str, budget: TenantBudget) -> PolicyVerdict:
        used = self._window_sum(self._token_spend, tenant_id)
        if used >= budget.daily_token_budget:
            return PolicyVerdict(False, BlockReason.BUDGET_EXHAUSTED)
        return PolicyVerdict(True)

    async def enter_call(self, tenant_id: str, budget: TenantBudget) -> PolicyVerdict:
        preflight = await self.check_token_budget(tenant_id, budget)
        if not preflight.allowed:
            return preflight
        in_flight = self._concurrent.get(tenant_id, 0)
        if in_flight >= budget.max_concurrent_requests:
            return PolicyVerdict(False, BlockReason.CONCURRENCY_LIMIT)
        self._concurrent[tenant_id] = in_flight + 1
        return PolicyVerdict(True)

    async def exit_call(self, tenant_id: str) -> None:
        in_flight = self._concurrent.get(tenant_id, 0)
        if in_flight > 0:
            self._concurrent[tenant_id] = in_flight - 1

    async def record_usage(
        self,
        tenant_id: str,
        budget: TenantBudget,
        *,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> tuple[AlertLevel, ...]:
        amount = max(prompt_tokens, 0) + max(completion_tokens, 0)
        self._record(self._token_spend, tenant_id, amount)
        used = self._window_sum(self._token_spend, tenant_id)
        return self._fire_alerts(tenant_id, budget, used)

    async def check_action(
        self, tenant_id: str, budget: TenantBudget, *, turn_index: int
    ) -> PolicyVerdict:
        if self._window_sum(self._action_spend, tenant_id) >= budget.max_actions_per_day:
            return PolicyVerdict(False, BlockReason.ACTION_LIMIT)
        recorded_turn, count = self._turn_actions.get(tenant_id, (-1, 0))
        in_turn = count if recorded_turn == turn_index else 0
        if in_turn >= budget.max_actions_per_turn:
            return PolicyVerdict(False, BlockReason.ACTION_LIMIT)
        return PolicyVerdict(True)

    async def record_action(self, tenant_id: str, *, turn_index: int) -> None:
        self._record(self._action_spend, tenant_id, 1)
        recorded_turn, count = self._turn_actions.get(tenant_id, (-1, 0))
        self._turn_actions[tenant_id] = (
            turn_index,
            count + 1 if recorded_turn == turn_index else 1,
        )

    def snapshot(self, tenant_id: str) -> UsageSnapshot:
        fired = self._fired.get(tenant_id, set())
        return UsageSnapshot(
            tenant_id=tenant_id,
            tokens_used=self._window_sum(self._token_spend, tenant_id),
            actions_committed=self._window_sum(self._action_spend, tenant_id),
            concurrent_calls=self._concurrent.get(tenant_id, 0),
            alerts_fired=tuple(
                level for level in (AlertLevel.WARN, AlertLevel.CRITICAL) if level in fired
            ),
        )

    def _fire_alerts(
        self, tenant_id: str, budget: TenantBudget, used: int
    ) -> tuple[AlertLevel, ...]:
        fired: list[AlertLevel] = []
        for level, threshold in (
            (AlertLevel.WARN, budget.spend_warn_threshold_tokens),
            (AlertLevel.CRITICAL, budget.spend_critical_threshold_tokens),
        ):
            if threshold is None or used < threshold:
                continue
            already = self._fired.setdefault(tenant_id, set())
            if level in already:
                continue
            already.add(level)
            fired.append(level)
            if self._metrics is not None:
                self._metrics.observe(
                    MetricName.BUDGET_ALERTS,
                    1,
                    labels={MetricLabelName.LEVEL.value: level.value},
                )
        return tuple(fired)
