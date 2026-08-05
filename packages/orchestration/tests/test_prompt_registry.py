"""The template registry: append-only, immutable, and code-reviewed.

`AI-003` keeps templates as versioned artifacts in the repository. This module
pins the two properties that makes that safe: a stored turn record's reference
keeps naming the same artifact forever (versions are never replaced), and a
change to a template is a new version, never an edit to one already registered.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import FrozenInstanceError

import pytest

from tenantchat.core.tenant import TenantPolicy
from tenantchat.orchestration.model import PromptRegion
from tenantchat.orchestration.prompts import (
    TemplateRegistry,
    TemplateRegistryError,
    TemplateSegment,
    TemplateVersion,
)
from tenantchat.orchestration.prompts.schema import BindingSchema, SlotKind, SlotSpec


def _bindings(policy: TenantPolicy, workflow: Mapping[str, object]) -> Mapping[str, str]:
    del policy, workflow
    return {"greeting": "Hello."}


def version(
    number: int,
    *,
    text: str = "Say {greeting}",
    greeting_max: int = 50,
) -> TemplateVersion:
    """A minimal one-slot template for registry behavior tests."""
    return TemplateVersion(
        template_id="synthetic",
        version=number,
        description=f"synthetic version {number}",
        segments=(TemplateSegment("line", PromptRegion.TRUSTED, text),),
        schema=BindingSchema((SlotSpec("greeting", SlotKind.TONE, max_chars=greeting_max),)),
        bindings=_bindings,
    )


def test_a_registered_version_is_current_and_resolvable() -> None:
    registry = TemplateRegistry()
    registry.register(version(1))

    assert registry.current("synthetic").ref == "synthetic@1"
    assert registry.resolve("synthetic", 1).ref == "synthetic@1"


def test_registering_the_same_version_twice_is_refused() -> None:
    registry = TemplateRegistry()
    registry.register(version(1))

    with pytest.raises(TemplateRegistryError, match="already registered"):
        registry.register(version(1))


def test_versions_must_be_registered_in_sequence() -> None:
    """A gap would leave the skipped version unregisterable forever, so the
    registry refuses to create one — versions are a strict history, not a
    counter that may be jumped.
    """
    registry = TemplateRegistry()
    registry.register(version(1))

    with pytest.raises(TemplateRegistryError, match="next version"):
        registry.register(version(3))


def test_changing_a_template_is_a_new_version_not_a_mutation() -> None:
    """The acceptance criterion: a template change must never rewrite a version
    a stored turn record already references.

    ``@1`` is registered, then the template is "changed" by registering ``@2``
    with different prose; the old reference still resolves to the original
    artifact, byte for byte.
    """
    registry = TemplateRegistry()
    registry.register(version(1, text="Say {greeting}"))
    registry.register(version(2, text="Say {greeting}, please."))

    held = registry.resolve("synthetic", 1)
    assert held.segments[0].text == "Say {greeting}"
    assert registry.current("synthetic").ref == "synthetic@2"


def test_a_template_version_is_immutable_after_registration() -> None:
    """Frozen values, so a reference held by a stored turn record can never be
    rewritten — not even by the code that registered it."""
    registry = TemplateRegistry()
    registry.register(version(1))

    with pytest.raises(FrozenInstanceError):
        registry.resolve("synthetic", 1).segments[0].text = "rewritten"  # type: ignore[misc]


def test_a_registration_cannot_reference_an_undeclared_slot() -> None:
    with pytest.raises(TemplateRegistryError, match="undeclared slot"):
        version(1, text="Say {missing}")


def test_duplicate_segment_ids_are_refused() -> None:
    with pytest.raises(TemplateRegistryError, match="duplicate segment id"):
        TemplateVersion(
            template_id="synthetic",
            version=1,
            description="duplicate ids",
            segments=(
                TemplateSegment("line", PromptRegion.TRUSTED, "one"),
                TemplateSegment("line", PromptRegion.TRUSTED, "two"),
            ),
            schema=BindingSchema(()),
            bindings=lambda policy, workflow: {},
        )


def test_unbalanced_braces_are_refused_at_registration() -> None:
    """A stray brace would raise at render time instead of registration time."""
    with pytest.raises(TemplateRegistryError, match="braces"):
        version(1, text="Say {greeting} and {10} off")


def test_versions_are_listed_ascending() -> None:
    registry = TemplateRegistry()
    registry.register(version(1))
    registry.register(version(2))

    assert [entry.version for entry in registry.versions("synthetic")] == [1, 2]


def test_an_unregistered_version_is_not_resolvable() -> None:
    with pytest.raises(TemplateRegistryError, match="not registered"):
        TemplateRegistry().resolve("synthetic", 1)
