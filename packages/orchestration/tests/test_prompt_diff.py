"""Canonical diffs between template versions (`FEAT-015`).

The diff is over template segments and declared binding schemas — never runtime
values — and it is deterministic: a pure function of the two versions, so the
viewer and a test assert on exactly the same output.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from tenantchat.core.catalog import ServiceCatalog
from tenantchat.core.tenant import PricingPolicy, TenantPolicy
from tenantchat.orchestration.model import MessageRole, PromptRegion
from tenantchat.orchestration.prompts import (
    DEFAULT_REGISTRY,
    BindingSchema,
    HistoryTurn,
    PromptBudget,
    PromptEvidence,
    SegmentChangeKind,
    SlotChangeKind,
    TemplateRegistry,
    TemplateSegment,
    TemplateVersion,
    assemble_prompt,
    diff_templates,
)
from tenantchat.orchestration.prompts.schema import SlotKind, SlotSpec


def _bindings(policy: TenantPolicy, workflow: Mapping[str, object]) -> Mapping[str, str]:
    del policy, workflow
    return {"greeting": "Hello.", "closing": "Goodbye."}


def make(
    number: int,
    *,
    text: str = "Say {greeting}\n{closing}",
    greeting_max: int = 50,
) -> TemplateVersion:
    return TemplateVersion(
        template_id="synthetic",
        version=number,
        description=f"synthetic version {number}",
        segments=(TemplateSegment("line", PromptRegion.TRUSTED, text),),
        schema=BindingSchema(
            (
                SlotSpec("greeting", SlotKind.TONE, max_chars=greeting_max),
                SlotSpec("closing", SlotKind.BUSINESS_FACT, max_chars=100),
            )
        ),
        bindings=_bindings,
    )


def test_identical_versions_produce_an_empty_diff() -> None:
    diff = diff_templates(make(1), make(2))

    assert not diff.changed
    assert [change.kind for change in diff.segments] == [SegmentChangeKind.UNCHANGED]
    assert all(change.kind is SlotChangeKind.UNCHANGED for change in diff.slots)
    assert diff.before_ref == "synthetic@1"
    assert diff.after_ref == "synthetic@2"


def test_a_text_change_is_a_segment_change_with_both_sides() -> None:
    diff = diff_templates(make(1, text="Say {greeting}"), make(2, text="Say {greeting}, please."))

    assert diff.changed
    (change,) = diff.segments
    assert change.kind is SegmentChangeKind.CHANGED
    assert change.before == "Say {greeting}"
    assert change.after == "Say {greeting}, please."
    assert diff.slots and all(slot.kind is SlotChangeKind.UNCHANGED for slot in diff.slots)


def test_adding_a_segment_is_reported_added() -> None:
    before = make(1)
    after = TemplateVersion(
        template_id="synthetic",
        version=2,
        description="added segment",
        segments=(
            TemplateSegment("line", PromptRegion.TRUSTED, "Say {greeting}\n{closing}"),
            TemplateSegment("signoff", PromptRegion.TRUSTED, "Signoff"),
        ),
        schema=BindingSchema(
            (
                SlotSpec("greeting", SlotKind.TONE, max_chars=50),
                SlotSpec("closing", SlotKind.BUSINESS_FACT, max_chars=100),
            )
        ),
        bindings=_bindings,
    )

    diff = diff_templates(before, after)

    assert [change.kind for change in diff.segments] == [
        SegmentChangeKind.UNCHANGED,
        SegmentChangeKind.ADDED,
    ]
    assert diff.segments[1].segment_id == "signoff"
    assert diff.segments[1].before is None
    assert diff.segments[1].after == "Signoff"


def test_removing_a_segment_is_reported_removed() -> None:
    before = TemplateVersion(
        template_id="synthetic",
        version=1,
        description="two segments",
        segments=(
            TemplateSegment("line", PromptRegion.TRUSTED, "Say {greeting}\n{closing}"),
            TemplateSegment("signoff", PromptRegion.TRUSTED, "Signoff"),
        ),
        schema=BindingSchema(
            (
                SlotSpec("greeting", SlotKind.TONE, max_chars=50),
                SlotSpec("closing", SlotKind.BUSINESS_FACT, max_chars=100),
            )
        ),
        bindings=_bindings,
    )
    after = make(2)

    diff = diff_templates(before, after)

    assert [change.kind for change in diff.segments] == [
        SegmentChangeKind.UNCHANGED,
        SegmentChangeKind.REMOVED,
    ]
    assert diff.segments[1].segment_id == "signoff"
    assert diff.segments[1].before == "Signoff"
    assert diff.segments[1].after is None


def test_a_changed_trust_marking_is_a_change_even_with_the_same_text() -> None:
    """A region flip is security-relevant, so it must surface in the diff even
    though the text is identical."""
    before = make(1)
    after = TemplateVersion(
        template_id="synthetic",
        version=2,
        description="untrusted line",
        segments=(TemplateSegment("line", PromptRegion.UNTRUSTED, "Say {greeting}\n{closing}"),),
        schema=BindingSchema(
            (
                SlotSpec("greeting", SlotKind.TONE, max_chars=50),
                SlotSpec("closing", SlotKind.BUSINESS_FACT, max_chars=100),
            )
        ),
        bindings=_bindings,
    )

    diff = diff_templates(before, after)

    assert diff.segments[0].kind is SegmentChangeKind.CHANGED


def test_binding_schema_changes_are_reported_per_slot() -> None:
    before = make(1)
    after = make(2, greeting_max=200)

    slots = {slot.name: slot for slot in diff_templates(before, after).slots}
    assert slots["greeting"].kind is SlotChangeKind.CHANGED
    assert slots["greeting"].before is not None
    assert slots["greeting"].before.max_chars == 50
    assert slots["greeting"].after is not None
    assert slots["greeting"].after.max_chars == 200
    assert slots["closing"].kind is SlotChangeKind.UNCHANGED


def test_slot_additions_and_removals_are_reported() -> None:
    before = make(1)
    after = TemplateVersion(
        template_id="synthetic",
        version=2,
        description="schema growth",
        segments=(TemplateSegment("line", PromptRegion.TRUSTED, "Say {greeting}\n{closing}"),),
        schema=BindingSchema(
            (
                SlotSpec("greeting", SlotKind.TONE, max_chars=50),
                SlotSpec("closing", SlotKind.BUSINESS_FACT, max_chars=100),
                SlotSpec("disclaimer", SlotKind.DISCLAIMER, max_chars=200, single_line=False),
            )
        ),
        bindings=_bindings,
    )

    slots = {slot.name: slot for slot in diff_templates(before, after).slots}
    assert slots["disclaimer"].kind is SlotChangeKind.ADDED
    assert slots["disclaimer"].before is None


def test_the_diff_is_deterministic() -> None:
    first = diff_templates(make(1), make(2, greeting_max=200))
    second = diff_templates(make(1), make(2, greeting_max=200))

    assert first == second
    assert first.segments == second.segments
    assert first.slots == second.slots


def test_runtime_values_never_appear_in_a_template_diff() -> None:
    """Tenant data is not template data: two different tenants' assembles of
    the same versions diff identically."""
    registry = TemplateRegistry()
    registry.register(make(1))
    registry.register(make(2, greeting_max=200))
    template = registry.current("synthetic")
    diff = diff_templates(registry.resolve("synthetic", 1), template)

    for phone in ("(555) 816-4420", "(555) 000-0000"):
        policy = TenantPolicy(
            tenant_id="clearview",
            name="Clearview",
            assistant_name="Clearview assistant",
            tagline="t",
            phone=phone,
            address="a",
            hours="h",
            catalog=ServiceCatalog.from_definitions([]),
            pricing_policy=PricingPolicy.NEVER,
            booking_enabled=False,
            lead_capture_enabled=False,
            proactive_lead_capture=False,
        )
        assemble_prompt(
            template,
            policy=policy,
            workflow={},
            history=(HistoryTurn(role=MessageRole.USER, content="hi"),),
            evidence=(PromptEvidence(source_id="d", title="t", content="c"),),
            budget=PromptBudget(),
        )

    assert "(555)" not in repr(diff)
    assert diff.segments == diff_templates(registry.resolve("synthetic", 1), template).segments


def test_diffing_two_templates_is_refused() -> None:
    other = TemplateVersion(
        template_id="other",
        version=1,
        description="another template",
        segments=(),
        schema=BindingSchema(()),
        bindings=lambda policy, workflow: {},
    )
    with pytest.raises(ValueError, match="different template ids"):
        diff_templates(make(1), other)


def test_the_dispatch_template_has_no_drift_against_its_registered_self() -> None:
    """The default registry and the module constant stay the same artifact."""
    diff = diff_templates(
        DEFAULT_REGISTRY.resolve("dispatch-system", 1),
        DEFAULT_REGISTRY.current("dispatch-system"),
    )
    assert not diff.changed
