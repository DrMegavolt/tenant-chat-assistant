"""The LLM-as-judge policy: only a validated judge may gate a release.

An LLM judge is an opaque scorer: its verdicts are only as trustworthy as the
agreement they have shown against human labels. This module is the mechanical
enforcement of the policy — a judge registers with a measured agreement on a
held-out set, and the release gate refuses to let any judge whose agreement
is unreported (or below the documented floor) block a release. An unvalidated
judge may still inform review: its verdicts ride the comparison report, just
never the gate decision.

There is no LLM judge inside the hermetic harness (no model is invoked), so
the registry is empty by default; the tests exercise the mechanism with
synthetic profiles, and a real judge ships by registering its agreement here.
"""

from __future__ import annotations

from dataclasses import dataclass

# A judge may gate only with at least this measured agreement over at least
# this many held-out human labels: a smaller sample cannot distinguish a good
# judge from a lucky one.
MIN_JUDGE_AGREEMENT = 0.8
MIN_HELD_OUT_SIZE = 20


@dataclass(frozen=True, slots=True)
class JudgeProfile:
    """One judge scorer's validation record.

    ``agreement`` is the measured fraction of verdicts matching human labels
    on ``held_out_size`` cases; ``None`` means no measurement exists, which is
    exactly the state that must not gate. ``validated_at`` names the review
    that recorded the measurement, for audit.
    """

    name: str
    agreement: float | None
    held_out_size: int
    validated_at: str | None = None

    def can_gate(self) -> bool:
        return (
            self.agreement is not None
            and self.agreement >= MIN_JUDGE_AGREEMENT
            and self.held_out_size >= MIN_HELD_OUT_SIZE
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "agreement": self.agreement,
            "held_out_size": self.held_out_size,
            "validated_at": self.validated_at,
            "gates": self.can_gate(),
        }


_JUDGES: dict[str, JudgeProfile] = {}


def register_judge(profile: JudgeProfile) -> None:
    """Register a judge scorer for the report and the gate."""
    _JUDGES[profile.name] = profile


def judges() -> tuple[JudgeProfile, ...]:
    """The registered judges, sorted for a stable report."""
    return tuple(sorted(_JUDGES.values(), key=lambda profile: profile.name))
