"""Time-bounded appointment opportunities, independent of any provider or tool.

A slot is what a booking names. Until `DATA-003`, a booking carried only the
display label the customer picked, so the domain could not tell "not in the
past" from "fine, just later", and could not know which calendar the label
belonged to. Giving the command a slot with a provider identity and an aware
start/end is what makes those verdicts decidable — and gives the reservation a
stable name that two racing attempts can collide on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class OfferedSlot:
    """One bookable window with a stable provider identity.

    ``id`` is the provider's own identifier, stable across offers so that
    ``get_availability`` last week and ``BookCommand`` today name the same
    window. ``start`` and ``end`` are timezone-aware, which is what makes "is
    this in the past?" and "does my booking overlap another's?" decidable
    without guessing a timezone.
    """

    id: str
    service_slug: str
    start: datetime
    end: datetime

    @property
    def label(self) -> str:
        """The text a customer (or the model) uses to name this slot.

        Kept here rather than by each consumer so that what is shown on the
        availability route, what the booking endpoint validates, and what the
        confirmation echoes are all one string. Built by hand instead of with
        ``strftime`` so the leading-zero and weekday/month spelling do not drift
        between platforms.
        """
        weekday = self.start.strftime("%a")
        month = self.start.strftime("%b")
        hour = self.start.hour % 12 or 12
        minute = f"{self.start.minute:02d}"
        period = "AM" if self.start.hour < 12 else "PM"
        return f"{weekday} {month} {self.start.day}, {hour}:{minute} {period}"
