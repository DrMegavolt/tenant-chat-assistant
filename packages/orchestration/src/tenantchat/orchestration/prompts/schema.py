"""Declared slots: the only place tenant input can enter a template.

A slot is a named, typed hole in a template. Tenants customize through slots —
tone, business facts, escalation rules, disclaimers — and through nothing else,
because assembly validates that the bound values are exactly the declared set
and that each value matches its slot's declared shape. Template *structure*
stays code; tenant input stays data that can neither add a slot (and so cannot
add an instruction section) nor change a segment's trust marking.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

_MAX_SLOT_NAME = 64

# Characters a slot value may never carry. A single-line slot rejects every
# control character; a multi-line slot tolerates line breaks but still rejects
# NUL and the other C0 controls that have no business in prompt text.
_ALWAYS_FORBIDDEN = "\x00\x01\x02\x03\x04\x05\x06\x07\x08\x0b\x0c\x0e\x0f"
_ALWAYS_FORBIDDEN += "\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1a\x1b\x1c\x1d\x1e\x1f\x7f"
_SINGLE_LINE_FORBIDDEN = _ALWAYS_FORBIDDEN + "\n\r\t"


class SlotKind(StrEnum):
    """What kind of tenant input a slot carries.

    The kinds name the customization the scope guarantees — tone, business
    facts, escalation rules, disclaimers — plus the code-derived rule and price
    text that a template may bind from policy booleans and approved prices.
    """

    TONE = "tone"
    BUSINESS_FACT = "business_fact"
    ESCALATION_RULE = "escalation_rule"
    DISCLAIMER = "disclaimer"
    POLICY_RULE = "policy_rule"
    PRICE_LIST = "price_list"


class PromptBindingError(ValueError):
    """A binding did not match the template's declared schema.

    Raised during assembly, so a tenant value that would reshape the prompt is
    a loud configuration failure, never a silent rewrite of what the model is
    told.
    """


@dataclass(frozen=True, slots=True)
class SlotSpec:
    """The declared shape of one slot: what a bound value may look like."""

    name: str
    kind: SlotKind
    max_chars: int
    single_line: bool = True

    def __post_init__(self) -> None:
        if not self.name or len(self.name) > _MAX_SLOT_NAME:
            raise ValueError(f"slot name {self.name!r} is empty or longer than {_MAX_SLOT_NAME}")
        if self.max_chars < 1:
            raise ValueError(f"slot {self.name!r} max_chars must be positive")


@dataclass(frozen=True, slots=True)
class BindingSchema:
    """The closed set of slots a template version accepts and requires.

    Registration checks that every placeholder in the template text names a
    declared slot; assembly checks that the bound values are exactly this set —
    nothing missing, nothing extra. A value for a name the schema does not
    declare is rejected, which is what keeps tenant input from introducing a
    new instruction section.
    """

    slots: tuple[SlotSpec, ...]

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for slot in self.slots:
            if slot.name in seen:
                raise ValueError(f"duplicate slot {slot.name!r}")
            seen.add(slot.name)

    def slot(self, name: str) -> SlotSpec | None:
        for slot in self.slots:
            if slot.name == name:
                return slot
        return None

    def validate(self, values: Mapping[str, str]) -> None:
        """Reject any binding that is not exactly the declared slots, filled to shape.

        Raises:
            PromptBindingError: a value names an undeclared slot, a declared
                slot is missing, a value exceeds its slot's length limit, or a
                single-line slot carries a control character.
        """
        declared = {slot.name for slot in self.slots}
        provided = set(values)
        missing = sorted(declared - provided)
        unknown = sorted(provided - declared)
        if missing or unknown:
            raise PromptBindingError(
                f"bindings do not match declared slots: missing {missing}, unknown {unknown}"
            )
        by_name = {slot.name: slot for slot in self.slots}
        for name, raw in values.items():
            slot = by_name[name]
            if len(raw) > slot.max_chars:
                raise PromptBindingError(
                    f"slot {name!r} is {len(raw)} chars, limit {slot.max_chars}"
                )
            forbidden = _SINGLE_LINE_FORBIDDEN if slot.single_line else _ALWAYS_FORBIDDEN
            for character in raw:
                if character in forbidden:
                    if slot.single_line:
                        raise PromptBindingError(
                            f"slot {name!r} must be one line and contains a control character"
                        )
                    raise PromptBindingError(
                        f"slot {name!r} contains the control character {ord(character):#x}"
                    )
