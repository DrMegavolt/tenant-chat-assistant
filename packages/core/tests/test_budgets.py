"""AI-002: the per-tenant budget ledger and content policy.

The acceptance criteria live here: an exceeded budget refuses predictably, cost
is attributable to a tenant without ever touching user content, and spend
alerts fire once per threshold. Every assertion is content-free — the ledger's
read surface is identifiers and counts, which is the point.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable, Mapping

import pytest

from tenantchat.core.budgets import (
    DEFAULT_TENANT_BUDGET,
    BudgetEnforcer,
    TenantBudget,
    UsageSnapshot,
    check_input,
    check_output,
)
from tenantchat.core.metrics import AlertLevel, BlockReason, MetricName
from tenantchat.core.tenant import TenantPolicy

TENANT = "clearview"
_TenantBuilder = Callable[..., TenantPolicy]


def _budget(**overrides: object) -> TenantBudget:
    return TenantBudget(**overrides)  # type: ignore[arg-type]


class _RecordingMetrics:
    def __init__(self) -> None:
        self.observations: list[tuple[str, float, dict[str, str]]] = []

    def observe(
        self,
        name: MetricName,
        value: float,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        self.observations.append((name.value, value, dict(labels or {})))


class TestTokenBudget:
    def test_a_tenant_within_budget_passes_preflight_and_reserves_concurrency(self) -> None:
        ledger = BudgetEnforcer()

        verdict = asyncio.run(ledger.enter_call(TENANT, DEFAULT_TENANT_BUDGET))

        assert verdict.allowed
        assert ledger.snapshot(TENANT).concurrent_calls == 1
        asyncio.run(ledger.exit_call(TENANT))
        assert ledger.snapshot(TENANT).concurrent_calls == 0

    def test_an_exhausted_token_budget_refuses_before_reserving(self) -> None:
        ledger = BudgetEnforcer()
        budget = _budget(daily_token_budget=100)
        asyncio.run(ledger.record_usage(TENANT, budget, prompt_tokens=60, completion_tokens=50))

        preflight = asyncio.run(ledger.check_token_budget(TENANT, budget))
        entered = asyncio.run(ledger.enter_call(TENANT, budget))

        assert not preflight.allowed
        assert preflight.block_reason is BlockReason.BUDGET_EXHAUSTED
        assert not entered.allowed
        assert entered.block_reason is BlockReason.BUDGET_EXHAUSTED
        assert ledger.snapshot(TENANT).concurrent_calls == 0

    def test_usage_is_attributable_to_a_tenant_without_content(self) -> None:
        ledger = BudgetEnforcer()
        asyncio.run(
            ledger.record_usage(
                "apex", DEFAULT_TENANT_BUDGET, prompt_tokens=10, completion_tokens=2
            )
        )
        asyncio.run(
            ledger.record_usage(
                TENANT, DEFAULT_TENANT_BUDGET, prompt_tokens=30, completion_tokens=4
            )
        )

        apex: UsageSnapshot = ledger.snapshot("apex")
        clearview: UsageSnapshot = ledger.snapshot(TENANT)
        assert apex.tokens_used == 12
        assert clearview.tokens_used == 34
        # The snapshot exposes identifiers and counts, and nothing else — there
        # is no field that could hold a message or a model answer.
        assert {field.name for field in dataclasses.fields(UsageSnapshot)} == {
            "tenant_id",
            "tokens_used",
            "actions_committed",
            "concurrent_calls",
            "alerts_fired",
        }

    def test_concurrency_is_capped_and_released(self) -> None:
        ledger = BudgetEnforcer()
        budget = _budget(max_concurrent_requests=2)

        first = asyncio.run(ledger.enter_call(TENANT, budget))
        second = asyncio.run(ledger.enter_call(TENANT, budget))
        third = asyncio.run(ledger.enter_call(TENANT, budget))

        assert first.allowed and second.allowed
        assert not third.allowed
        assert third.block_reason is BlockReason.CONCURRENCY_LIMIT
        assert ledger.snapshot(TENANT).concurrent_calls == 2

        asyncio.run(ledger.exit_call(TENANT))
        later = asyncio.run(ledger.enter_call(TENANT, budget))
        assert later.allowed
        assert ledger.snapshot(TENANT).concurrent_calls == 2


class TestActionBudget:
    def test_the_per_turn_action_cap_refuses_the_next_action(self) -> None:
        ledger = BudgetEnforcer()
        budget = _budget(max_actions_per_turn=2)

        assert asyncio.run(ledger.check_action(TENANT, budget, turn_index=7)).allowed
        asyncio.run(ledger.record_action(TENANT, turn_index=7))
        assert asyncio.run(ledger.check_action(TENANT, budget, turn_index=7)).allowed
        asyncio.run(ledger.record_action(TENANT, turn_index=7))

        refused = asyncio.run(ledger.check_action(TENANT, budget, turn_index=7))
        assert not refused.allowed
        assert refused.block_reason is BlockReason.ACTION_LIMIT

    def test_the_per_turn_cap_resets_on_a_fresh_turn(self) -> None:
        ledger = BudgetEnforcer()
        budget = _budget(max_actions_per_turn=1)
        asyncio.run(ledger.record_action(TENANT, turn_index=3))
        assert not asyncio.run(ledger.check_action(TENANT, budget, turn_index=3)).allowed
        assert asyncio.run(ledger.check_action(TENANT, budget, turn_index=4)).allowed

    def test_the_daily_action_cap_is_independent_of_turns(self) -> None:
        ledger = BudgetEnforcer()
        budget = _budget(max_actions_per_day=1, max_actions_per_turn=100)
        asyncio.run(ledger.record_action(TENANT, turn_index=1))
        refused = asyncio.run(ledger.check_action(TENANT, budget, turn_index=2))
        assert not refused.allowed
        assert refused.block_reason is BlockReason.ACTION_LIMIT


class TestSpendAlerts:
    def test_a_crossed_threshold_fires_once_and_records_a_metric(self) -> None:
        metrics = _RecordingMetrics()
        ledger = BudgetEnforcer(metrics=metrics)
        budget = _budget(spend_warn_threshold_tokens=100, spend_critical_threshold_tokens=200)

        first = asyncio.run(
            ledger.record_usage(TENANT, budget, prompt_tokens=60, completion_tokens=50)
        )
        second = asyncio.run(
            ledger.record_usage(TENANT, budget, prompt_tokens=60, completion_tokens=50)
        )
        third = asyncio.run(
            ledger.record_usage(TENANT, budget, prompt_tokens=60, completion_tokens=50)
        )

        # 110 crosses warn; 220 crosses critical (warn already fired); 330
        # crosses nothing new, so no alert re-fires.
        assert first == (AlertLevel.WARN,)
        assert second == (AlertLevel.CRITICAL,)
        assert third == ()
        alerts = [obs for obs in metrics.observations if obs[0] == MetricName.BUDGET_ALERTS.value]
        assert sorted(labels["level"] for _, _, labels in alerts) == ["critical", "warn"]
        assert ledger.snapshot(TENANT).alerts_fired == (AlertLevel.WARN, AlertLevel.CRITICAL)

    def test_no_thresholds_means_no_alerts(self) -> None:
        ledger = BudgetEnforcer()
        fired = asyncio.run(
            ledger.record_usage(
                TENANT, DEFAULT_TENANT_BUDGET, prompt_tokens=10, completion_tokens=0
            )
        )
        assert fired == ()


class TestContentPolicy:
    def test_an_over_length_message_is_refused_as_input(self) -> None:
        budget = _budget(max_message_chars=10)
        verdict = check_input("a message that is far too long for this tenant", budget)
        assert not verdict.allowed
        assert verdict.block_reason is BlockReason.INPUT_TOO_LONG

    def test_a_binary_message_is_refused_as_input(self) -> None:
        verdict = check_input("hello\x00world", DEFAULT_TENANT_BUDGET)
        assert not verdict.allowed
        assert verdict.block_reason is BlockReason.INPUT_BINARY

    def test_a_normal_message_passes(self) -> None:
        verdict = check_input("What are your hours?", DEFAULT_TENANT_BUDGET)
        assert verdict.allowed

    def test_over_length_model_output_is_refused_whole(self) -> None:
        budget = _budget(max_output_chars=20)
        verdict = check_output("This answer is far longer than the tenant allows.", budget)
        assert not verdict.allowed
        assert verdict.block_reason is BlockReason.OUTPUT_TOO_LONG

    def test_in_range_output_passes(self) -> None:
        verdict = check_output("Open until 7pm.", DEFAULT_TENANT_BUDGET)
        assert verdict.allowed


class TestBudgetConfiguration:
    def test_a_tenant_without_a_budget_still_has_limits(self, build_tenant: _TenantBuilder) -> None:
        """``None`` on the policy means the platform default, never unlimited."""
        policy = build_tenant()
        assert policy.budgets is None
        assert DEFAULT_TENANT_BUDGET.daily_token_budget >= 1

    def test_a_tenant_carries_its_own_budget(self, build_tenant: _TenantBuilder) -> None:
        policy = build_tenant(budgets=TenantBudget(daily_token_budget=500))
        assert policy.budgets is not None
        assert policy.budgets.daily_token_budget == 500

    def test_an_invalid_budget_is_refused(self) -> None:
        with pytest.raises(ValueError):
            TenantBudget(daily_token_budget=0)
        with pytest.raises(ValueError):
            TenantBudget(max_concurrent_requests=0)
        with pytest.raises(ValueError):
            TenantBudget(max_actions_per_turn=0)
