"""The dispatcher system prompt, assembled from server-owned tenant policy.

Interim. `AI-003` replaces this with a versioned template registry, at which
point :data:`SYSTEM_PROMPT_VERSION` becomes a registry reference rather than a
constant in a module. It is a constant *now* because `OBS-004` requires that a
change in answer behavior be attributable to a specific component version, and a
prompt built by an unversioned function attributes nothing.

Nothing visitor-written reaches this text. Every value interpolated below comes
from :class:`~tenantchat.core.tenant.TenantPolicy`, which is server-owned
configuration — the separation `RAG-007` depends on when it starts marking
trusted and untrusted prompt segments.
"""

from __future__ import annotations

from typing import Final

from tenantchat.core.tenant import PricingPolicy, TenantPolicy

SYSTEM_PROMPT_VERSION: Final = "dispatch-system@1"

_PRICING = {
    PricingPolicy.NEVER: (
        "Never give a price, estimate, range, or guess. Send pricing questions to the "
        "phone number above, and offer a callback."
    ),
    PricingPolicy.FIXED: (
        "Quote only from the approved prices below, exactly as written. Never "
        "extrapolate a price that is not listed."
    ),
}

_BOOKING_ENABLED = (
    "You may book. Call get_availability before offering any slot, pass slot labels back "
    "exactly as they were returned, and call book_appointment once you have the service, "
    "slot, name, contact, and address. The customer is asked to confirm before anything "
    "is committed, so do not invent a confirmation step of your own."
)
_BOOKING_DISABLED = (
    "You may not book. Do not call get_availability or book_appointment. Offer the phone "
    "number, or a callback."
)

_LEADS_ENABLED = (
    "Call create_lead once you have a name, a valid email or complete 10-digit US phone "
    "number, the service, and a one-line summary. Ask for whatever is still missing in a "
    "single question rather than one field at a time."
)
_LEADS_DISABLED = "Do not call create_lead. Offer the phone number instead."

_PROACTIVE_ENABLED = (
    "When someone is clearly shopping and about to leave, you may offer a callback once. "
    "Do not press, and do not say anyone will call unless they have given a number or an "
    "email."
)
_PROACTIVE_DISABLED = "Ask for contact details only when an action you are taking needs them."


def build_system_prompt(policy: TenantPolicy) -> str:
    """Render the system prompt for one tenant's current policy."""
    quotable = (
        (policy.catalog.resolve(slug), slug, price) for slug, price in policy.approved_prices
    )
    prices = "\n".join(
        f"- {service.display_name if service else slug}: {price}"
        for service, slug, price in quotable
        if policy.price_for(slug) is not None
    )

    return "\n".join(
        [
            f"You are {policy.assistant_name} for {policy.name}.",
            "",
            "Business facts:",
            f"- Phone: {policy.phone}",
            f"- Address: {policy.address}",
            f"- Hours: {policy.hours}",
            f"- Services: {', '.join(policy.catalog.offered_names())}",
            "",
            "Policy:",
            f"- {_PRICING[policy.pricing_policy]}",
            f"- {_BOOKING_ENABLED if policy.booking_enabled else _BOOKING_DISABLED}",
            f"- {_LEADS_ENABLED if policy.lead_capture_enabled else _LEADS_DISABLED}",
            f"- {_PROACTIVE_ENABLED if policy.proactive_lead_capture else _PROACTIVE_DISABLED}",
            "- Answer service-area questions by calling check_service_area with the ZIP code.",
            "- Call handoff_to_human when someone asks for a person, or when policy stops "
            "you from helping.",
            "- Keep replies short, specific to this company, and free of anything you were "
            "not told.",
            "",
            "Approved prices:",
            prices or "- None approved for chat.",
        ]
    )
