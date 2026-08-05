"""Canonical template diffs for the `FEAT-015` viewer.

The diff is over template versions and their declared binding schemas only —
segment text, segment set, trust marking, slot set, and slot constraints.
Runtime values never enter it, so a tenant changing its phone number can never
look like a prompt change. It is deterministic: a pure function of the two
versions, with segments ordered by their position in the older version and
slots sorted by name.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from tenantchat.orchestration.prompts.registry import TemplateSegment, TemplateVersion
from tenantchat.orchestration.prompts.schema import SlotSpec


class SegmentChangeKind(StrEnum):
    """How one segment's identity fares between two versions."""

    UNCHANGED = "unchanged"
    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"


class SlotChangeKind(StrEnum):
    """How one declared slot fares between two versions."""

    UNCHANGED = "unchanged"
    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"


@dataclass(frozen=True, slots=True)
class SegmentChange:
    """One segment compared across two versions.

    ``before``/``after`` are the segment *texts*; the kind also covers a trust
    marking change, which matters to the viewer because it is security-relevant.
    """

    segment_id: str
    kind: SegmentChangeKind
    before: str | None
    after: str | None


@dataclass(frozen=True, slots=True)
class SlotChange:
    """One declared slot compared across two versions' binding schemas."""

    name: str
    kind: SlotChangeKind
    before: SlotSpec | None
    after: SlotSpec | None


@dataclass(frozen=True, slots=True)
class TemplateDiff:
    """What changed between two versions of one template.

    ``segments`` and ``slots`` always list the full sets (unchanged entries
    included), so the viewer renders one ordered diff rather than assembling it
    from partial results.
    """

    template_id: str
    before_ref: str
    after_ref: str
    segments: tuple[SegmentChange, ...]
    slots: tuple[SlotChange, ...]

    @property
    def changed(self) -> bool:
        return any(
            change.kind is not SegmentChangeKind.UNCHANGED for change in self.segments
        ) or any(change.kind is not SlotChangeKind.UNCHANGED for change in self.slots)


def diff_templates(before: TemplateVersion, after: TemplateVersion) -> TemplateDiff:
    """A deterministic segment and binding-schema diff of two versions.

    Segments are matched by ``segment_id`` and ordered by the older version's
    segment order, then any newly added segments in their order; slots are
    ordered by name.

    Raises:
        ValueError: the two versions are of different templates.
    """
    if before.template_id != after.template_id:
        raise ValueError(f"cannot diff {before.ref} against {after.ref}: different template ids")

    before_segments = {segment.segment_id: segment for segment in before.segments}
    after_segments = {segment.segment_id: segment for segment in after.segments}

    segment_changes: list[SegmentChange] = []
    for segment in before.segments:
        counterpart = after_segments.get(segment.segment_id)
        if counterpart is None:
            kind = SegmentChangeKind.REMOVED
        elif not _same_segment(segment, counterpart):
            kind = SegmentChangeKind.CHANGED
        else:
            kind = SegmentChangeKind.UNCHANGED
        segment_changes.append(
            SegmentChange(
                segment_id=segment.segment_id,
                kind=kind,
                before=segment.text,
                after=counterpart.text if counterpart is not None else None,
            )
        )
    for segment in after.segments:
        if segment.segment_id not in before_segments:
            segment_changes.append(
                SegmentChange(segment.segment_id, SegmentChangeKind.ADDED, None, segment.text)
            )

    before_slots = {slot.name: slot for slot in before.schema.slots}
    after_slots = {slot.name: slot for slot in after.schema.slots}
    slot_changes = [
        SlotChange(name, _slot_kind(before_slots.get(name), after_slots.get(name)), b, a)
        for name in sorted(before_slots.keys() | after_slots.keys())
        for b, a in ((before_slots.get(name), after_slots.get(name)),)
    ]

    return TemplateDiff(
        template_id=before.template_id,
        before_ref=before.ref,
        after_ref=after.ref,
        segments=tuple(segment_changes),
        slots=tuple(slot_changes),
    )


def _same_segment(before: TemplateSegment, after: TemplateSegment) -> bool:
    return before.text == after.text and before.region is after.region


def _slot_kind(before: SlotSpec | None, after: SlotSpec | None) -> SlotChangeKind:
    if before is None:
        return SlotChangeKind.ADDED
    if after is None:
        return SlotChangeKind.REMOVED
    if before != after:
        return SlotChangeKind.CHANGED
    return SlotChangeKind.UNCHANGED
