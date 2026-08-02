#!/usr/bin/env python3
"""Dependency-free prototype backend for the tenant chat widget.

The server exposes an OpenAI-compatible agent loop:

- POST /api/chat accepts tenant id, session id, and chat messages.
- The backend calls a local OpenAI-compatible API at localhost:1234.
- Tool calls are executed by this backend, not by the browser.
- Static files are served from the current directory for convenience.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, cast

from internal_auth import (
    internal_bearer_headers,
    load_internal_credentials,
    reject_external_credential_reuse,
)
from runtime_security import (
    is_production,
    load_openai_compatible_settings,
    openai_request_headers,
    require_production_environment,
)


ROOT = Path(__file__).resolve().parent
STATIC_ROOT = ROOT / "frontend" / "public"
CHATS_DIR = Path(os.environ.get("CHATS_DIR", ROOT / "chats"))
HOST = os.environ.get("CHAT_HOST", "127.0.0.1")
PORT = int(os.environ.get("CHAT_PORT", "8000"))
ADMIN_PORT = int(os.environ.get("CHAT_ADMIN_PORT", "8004"))
LLM_SETTINGS = load_openai_compatible_settings(local_base_url="http://localhost:1234/v1")
LLM_BASE_URL = LLM_SETTINGS.base_url
LLM_MODEL = LLM_SETTINGS.model
LLM_API_KEY = LLM_SETTINGS.api_key
LLM_TIMEOUT_SECONDS = LLM_SETTINGS.timeout_seconds
FINANCING_AGENT_URL = os.environ.get("FINANCING_AGENT_URL", "")
INTERNAL_CREDENTIALS = load_internal_credentials(
    {"CHAT_TO_FINANCING_TOKEN": "chat-backend"},
    required=bool(FINANCING_AGENT_URL),
)
FINANCING_AGENT_TOKEN = INTERNAL_CREDENTIALS.get("chat-backend")
reject_external_credential_reuse(INTERNAL_CREDENTIALS, ("LLM_API_KEY",))
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DATABASE_INIT_RETRIES = int(os.environ.get("DATABASE_INIT_RETRIES", "30"))
MAX_TOOL_ROUNDS = 4

require_production_environment(("DATABASE_URL",))


TenantConfig = Dict[str, Any]


TENANTS: Dict[str, TenantConfig] = {
    "apex": {
        "name": "Apex Home Services",
        "assistantName": "Apex assistant",
        "tagline": "Phone-first service desk",
        "phone": "(555) 214-0800",
        "address": "2100 Harbor Street, Seattle, WA 98101",
        "hours": "Mon-Fri 8:00 AM-6:00 PM, Sat 9:00 AM-2:00 PM",
        "pricingPolicy": "never",
        "bookingEnabled": False,
        "leadCaptureEnabled": True,
        "proactiveLeadCapture": True,
        "services": ["HVAC", "Electrical", "Plumbing"],
        "serviceZips": ["98101", "98102", "98103", "98104", "98105"],
        "prices": {},
        "availability": {},
        "quickActions": [
            "What are your hours?",
            "Do you serve 98103?",
            "How much is HVAC repair?",
            "Can I book electrical?",
            "Have someone call me",
        ],
        "site": {
            "headline": "Reliable home service with a dispatcher on every request.",
            "description": (
                "Apex wants the assistant to answer basic business questions, "
                "check service areas, and route pricing or booking questions to the phone team."
            ),
        },
    },
    "clearview": {
        "name": "Clearview Property Care",
        "assistantName": "Clearview assistant",
        "tagline": "Pricing and booking enabled",
        "phone": "(555) 816-4420",
        "address": "480 Lakeview Avenue, Portland, OR 97205",
        "hours": "Daily 7:00 AM-7:00 PM",
        "pricingPolicy": "fixed",
        "bookingEnabled": True,
        "leadCaptureEnabled": True,
        "proactiveLeadCapture": True,
        "services": ["Window Cleaning", "HVAC", "Electrical"],
        "serviceZips": ["97035", "97201", "97202", "97203", "97204", "97205"],
        "prices": {
            "window cleaning": "$150/hour, 2 hour minimum",
            "hvac": "$120 diagnostic visit, repairs quoted after inspection",
            "electrical": "$140 diagnostic visit, panel work quoted after inspection",
        },
        "availability": {
            "window cleaning": [
                "Mon Jul 1, 9:00 AM",
                "Tue Jul 2, 1:30 PM",
                "Thu Jul 4, 10:30 AM",
            ],
            "hvac": ["Mon Jul 1, 2:00 PM", "Wed Jul 3, 11:00 AM", "Fri Jul 5, 9:30 AM"],
            "electrical": [
                "Tue Jul 2, 8:30 AM",
                "Wed Jul 3, 3:00 PM",
                "Fri Jul 5, 1:00 PM",
            ],
        },
        "quickActions": [
            "What does window cleaning cost?",
            "Do you serve 97205?",
            "Book HVAC",
            "Electrical availability",
            "Request a follow-up",
        ],
        "site": {
            "headline": "Book property care with clear prices and live availability.",
            "description": (
                "Clearview allows the assistant to quote approved prices, check ZIP codes, "
                "separate service categories, and offer booking slots."
            ),
        },
    },
}


SESSIONS: Dict[str, Dict[str, Any]] = {}
LEADS: List[Dict[str, Any]] = []
BOOKINGS: List[Dict[str, Any]] = []
MESSAGE_COUNTER = 0
POSTGRES_READY = False
ARCHIVE_AFTER_SECONDS = 300


TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "check_service_area",
            "description": "Check whether a tenant serves a customer ZIP code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "zip": {
                        "type": "string",
                        "description": "A five digit US ZIP code, such as 97205.",
                    }
                },
                "required": ["zip"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_availability",
            "description": "Get available appointment slots for one configured service category.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {
                        "type": "string",
                        "description": "Configured service category, for example hvac or electrical.",
                    }
                },
                "required": ["service"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_appointment",
            "description": "Book a slot after the user explicitly confirms it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string"},
                    "slot": {"type": "string"},
                    "customer_name": {"type": "string"},
                    "customer_phone_or_email": {"type": "string"},
                    "address": {"type": "string"},
                },
                "required": ["service", "slot", "customer_name", "customer_phone_or_email", "address"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_lead",
            "description": (
                "Create a sales or service follow-up lead after collecting enough "
                "contact and request details from the user."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_name": {"type": "string"},
                    "customer_phone_or_email": {"type": "string"},
                    "service": {
                        "type": "string",
                        "description": "Configured service category or best available description.",
                    },
                    "address_or_zip": {"type": "string"},
                    "urgency": {
                        "type": "string",
                        "description": "One of emergency, today, this_week, flexible, or unknown.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Short summary of what the customer needs.",
                    },
                },
                "required": [
                    "customer_name",
                    "customer_phone_or_email",
                    "service",
                    "summary",
                ],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "handoff_to_human",
            "description": "Create a human handoff when the assistant is not allowed to answer or act.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["reason", "summary"],
                "additionalProperties": False,
            },
        },
    },
]


def public_tenant_config(tenant: TenantConfig) -> TenantConfig:
    allowed = [
        "name",
        "assistantName",
        "tagline",
        "phone",
        "address",
        "hours",
        "pricingPolicy",
        "bookingEnabled",
        "leadCaptureEnabled",
        "proactiveLeadCapture",
        "services",
        "quickActions",
        "site",
    ]
    return {key: tenant[key] for key in allowed}


def normalize_service(tenant: TenantConfig, raw_service: str) -> Optional[str]:
    value = raw_service.strip().lower()
    for service in tenant["services"]:
        canonical = service.lower()
        if value == canonical or value in canonical or canonical in value:
            return cast(str, canonical)
    return None


def normalize_slot_text(value: str) -> str:
    normalized = value.strip().lower()
    replacements = {
        "monday": "mon",
        "tuesday": "tue",
        "wednesday": "wed",
        "thursday": "thu",
        "friday": "fri",
        "saturday": "sat",
        "sunday": "sun",
        "january": "jan",
        "february": "feb",
        "march": "mar",
        "april": "apr",
        "june": "jun",
        "july": "jul",
        "august": "aug",
        "september": "sep",
        "sept": "sep",
        "october": "oct",
        "november": "nov",
        "december": "dec",
    }
    for source, target in replacements.items():
        normalized = re.sub(rf"\b{source}\b", target, normalized)
    normalized = re.sub(r"\bat\b", " ", normalized)
    normalized = re.sub(r"[,]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def find_matching_slot(requested_slot: str, available_slots: List[str]) -> Optional[str]:
    requested = normalize_slot_text(requested_slot)
    for slot in available_slots:
        if normalize_slot_text(slot) == requested:
            return slot
    return None


def now_seconds() -> int:
    return int(time.time())


def safe_file_stem(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", value)
    return cleaned[:120] or "chat"


def next_message_id() -> str:
    global MESSAGE_COUNTER
    MESSAGE_COUNTER += 1
    return f"msg-{MESSAGE_COUNTER}"


def ensure_session(tenant_id: str, session_id: str) -> Dict[str, Any]:
    tenant = TENANTS[tenant_id]
    if session_id not in SESSIONS:
        timestamp = now_seconds()
        SESSIONS[session_id] = {
            "sessionId": session_id,
            "tenantId": tenant_id,
            "tenantName": tenant["name"],
            "createdAt": timestamp,
            "updatedAt": timestamp,
            "messages": [],
            "toolEvents": [],
            "adminNotes": [],
            "status": "active",
            "outcome": "active",
        }
    session = SESSIONS[session_id]
    session["tenantId"] = tenant_id
    session["tenantName"] = tenant["name"]
    session["updatedAt"] = now_seconds()
    update_session_status(session)
    return session


def set_session_messages(session: Dict[str, Any], messages: List[Dict[str, str]]) -> None:
    existing_admin_messages = [
        message for message in session.get("messages", []) if message.get("source") == "admin"
    ]
    normalized_messages = []
    timestamp = now_seconds()
    for message in messages:
        normalized_messages.append(
            {
                "id": next_message_id(),
                "role": message["role"],
                "content": message["content"],
                "source": message.get("source", "visitor" if message["role"] == "user" else "assistant"),
                "createdAt": timestamp,
            }
        )
    session["messages"] = merge_admin_messages(normalized_messages, existing_admin_messages)
    session["updatedAt"] = timestamp
    update_session_status(session)
    persist_session(session)


def merge_admin_messages(
    messages: List[Dict[str, Any]], admin_messages: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    merged = list(messages)
    known = {(message["role"], message["content"], message.get("source")) for message in merged}
    for message in admin_messages:
        key = (message["role"], message["content"], message.get("source"))
        if key not in known:
            merged.append(message)
    return sorted(merged, key=lambda item: item.get("createdAt", 0))


def append_session_message(
    session: Dict[str, Any],
    role: str,
    content: str,
    source: str,
) -> Dict[str, Any]:
    message = {
        "id": next_message_id(),
        "role": role,
        "content": content,
        "source": source,
        "createdAt": now_seconds(),
    }
    session.setdefault("messages", []).append(message)
    session["updatedAt"] = message["createdAt"]
    update_session_status(session)
    persist_session(session)
    return message


def append_tool_events(session: Dict[str, Any], events: List[Dict[str, Any]]) -> None:
    timestamp = now_seconds()
    stored_events = session.setdefault("toolEvents", [])
    for event in events:
        stored_events.append(
            {
                "id": f"tool-{len(stored_events) + 1}",
                "name": event["name"],
                "arguments": event.get("arguments", {}),
                "result": event.get("result", {}),
                "createdAt": timestamp,
            }
        )
    if events:
        session["updatedAt"] = timestamp
    update_session_status(session)
    persist_session(session)


def leads_for_session(session_id: str) -> List[Dict[str, Any]]:
    return [lead for lead in LEADS if lead.get("sessionId") == session_id]


def bookings_for_session(session_id: str) -> List[Dict[str, Any]]:
    return [booking for booking in BOOKINGS if booking.get("sessionId") == session_id]


def infer_outcome(session: Dict[str, Any]) -> str:
    tool_events = session.get("toolEvents", [])
    messages = session.get("messages", [])
    if any(
        event.get("name") == "book_appointment"
        and event.get("result", {}).get("status") == "confirmed"
        for event in tool_events
    ) or bookings_for_session(session["sessionId"]):
        return "booked"
    if leads_for_session(session["sessionId"]):
        return "lead"
    if any(
        event.get("name") == "handoff_to_human"
        and event.get("result", {}).get("status") == "created"
        for event in tool_events
    ):
        return "handoff"
    if not any(message.get("role") == "user" for message in messages):
        return "empty"
    user_messages = [
        message.get("content", "").lower()
        for message in messages[-4:]
        if message.get("role") == "user"
    ]
    if any(
        re.search(r"\b(thanks|thank you|that'?s all|no thanks|all set|done)\b", message)
        for message in user_messages
    ):
        return "completed"
    if now_seconds() - session.get("updatedAt", 0) >= ARCHIVE_AFTER_SECONDS:
        return "abandoned"
    return "active"


def update_session_status(session: Dict[str, Any]) -> None:
    outcome = infer_outcome(session)
    session["outcome"] = outcome
    session["status"] = "active" if outcome == "active" else "archived"


def using_postgres_storage() -> bool:
    return bool(DATABASE_URL)


def storage_description() -> str:
    if using_postgres_storage():
        return "Postgres table chat_sessions(payload jsonb)"
    return str(CHATS_DIR)


def build_session_payload(session: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "schemaVersion": 1,
        "savedAt": now_seconds(),
        "session": session,
        "leads": leads_for_session(session["sessionId"]),
        "bookings": bookings_for_session(session["sessionId"]),
    }


def ensure_postgres_schema() -> None:
    global POSTGRES_READY
    if POSTGRES_READY or not using_postgres_storage():
        return

    last_error: Optional[BaseException] = None
    for attempt in range(1, DATABASE_INIT_RETRIES + 1):
        try:
            import psycopg

            with psycopg.connect(DATABASE_URL) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS chat_sessions (
                            session_id text PRIMARY KEY,
                            tenant_id text NOT NULL,
                            status text NOT NULL,
                            outcome text NOT NULL,
                            created_at_epoch bigint NOT NULL,
                            updated_at_epoch bigint NOT NULL,
                            saved_at_epoch bigint NOT NULL,
                            payload jsonb NOT NULL
                        )
                        """
                    )
                    cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS chat_sessions_tenant_updated_idx
                        ON chat_sessions (tenant_id, updated_at_epoch DESC)
                        """
                    )
                    cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS chat_sessions_outcome_idx
                        ON chat_sessions (outcome)
                        """
                    )
            POSTGRES_READY = True
            return
        except Exception as error:  # pragma: no cover - depends on external DB startup.
            last_error = error
            time.sleep(min(5, attempt))

    raise RuntimeError(f"Postgres chat storage is unavailable: {last_error}")


def persist_session_postgres(session: Dict[str, Any], payload: Dict[str, Any]) -> None:
    ensure_postgres_schema()
    import psycopg
    from psycopg.types.json import Jsonb

    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO chat_sessions (
                    session_id,
                    tenant_id,
                    status,
                    outcome,
                    created_at_epoch,
                    updated_at_epoch,
                    saved_at_epoch,
                    payload
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (session_id) DO UPDATE SET
                    tenant_id = EXCLUDED.tenant_id,
                    status = EXCLUDED.status,
                    outcome = EXCLUDED.outcome,
                    created_at_epoch = EXCLUDED.created_at_epoch,
                    updated_at_epoch = EXCLUDED.updated_at_epoch,
                    saved_at_epoch = EXCLUDED.saved_at_epoch,
                    payload = EXCLUDED.payload
                """,
                (
                    session["sessionId"],
                    session["tenantId"],
                    session.get("status", "active"),
                    session.get("outcome", "active"),
                    int(session.get("createdAt", now_seconds())),
                    int(session.get("updatedAt", now_seconds())),
                    int(payload["savedAt"]),
                    Jsonb(payload),
                ),
            )


def persist_session_file(session: Dict[str, Any], payload: Dict[str, Any]) -> None:
    CHATS_DIR.mkdir(exist_ok=True)
    file_path = CHATS_DIR / f"{safe_file_stem(session['sessionId'])}.json"
    tmp_path = file_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp_path.replace(file_path)


def persist_session(session: Dict[str, Any]) -> None:
    update_session_status(session)
    payload = build_session_payload(session)
    if using_postgres_storage():
        persist_session_postgres(session, payload)
        return
    persist_session_file(session, payload)


def persist_session_id(session_id: str) -> None:
    session = SESSIONS.get(session_id)
    if session:
        persist_session(session)


def load_saved_payload(payload: Dict[str, Any]) -> None:
    session = payload.get("session")
    if not isinstance(session, dict) or not session.get("sessionId"):
        return
    SESSIONS[session["sessionId"]] = session
    for lead in payload.get("leads", []):
        if isinstance(lead, dict) and not any(
            existing.get("leadId") == lead.get("leadId") for existing in LEADS
        ):
            LEADS.append(lead)
    for booking in payload.get("bookings", []):
        if isinstance(booking, dict) and not any(
            existing.get("bookingId") == booking.get("bookingId") for existing in BOOKINGS
        ):
            BOOKINGS.append(booking)


def load_saved_chats_postgres() -> None:
    ensure_postgres_schema()
    import psycopg

    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT payload
                FROM chat_sessions
                ORDER BY updated_at_epoch DESC
                """
            )
            for (payload,) in cursor.fetchall():
                if isinstance(payload, dict):
                    load_saved_payload(payload)


def load_saved_chats_file() -> None:
    CHATS_DIR.mkdir(exist_ok=True)
    for file_path in CHATS_DIR.glob("*.json"):
        try:
            load_saved_payload(json.loads(file_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue


def load_saved_chats() -> None:
    if using_postgres_storage():
        load_saved_chats_postgres()
    else:
        load_saved_chats_file()
    for session in SESSIONS.values():
        update_session_status(session)


def session_summary(session: Dict[str, Any]) -> Dict[str, Any]:
    previous_status = (session.get("status"), session.get("outcome"))
    update_session_status(session)
    if previous_status != (session.get("status"), session.get("outcome")):
        persist_session(session)
    messages = session.get("messages", [])
    last_message = messages[-1] if messages else None
    return {
        "sessionId": session["sessionId"],
        "tenantId": session["tenantId"],
        "tenantName": session["tenantName"],
        "createdAt": session["createdAt"],
        "updatedAt": session["updatedAt"],
        "active": now_seconds() - session["updatedAt"] < ARCHIVE_AFTER_SECONDS,
        "status": session.get("status", "active"),
        "outcome": session.get("outcome", "active"),
        "messageCount": len(messages),
        "toolEventCount": len(session.get("toolEvents", [])),
        "leadCount": len(leads_for_session(session["sessionId"])),
        "lastMessage": last_message,
    }


def session_detail(session: Dict[str, Any]) -> Dict[str, Any]:
    detail = dict(session_summary(session))
    detail["messages"] = session.get("messages", [])
    detail["toolEvents"] = session.get("toolEvents", [])
    detail["leads"] = leads_for_session(session["sessionId"])
    detail["bookings"] = bookings_for_session(session["sessionId"])
    return detail


def prometheus_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_chat_metrics() -> str:
    outcome_counts: Dict[Tuple[str, str], int] = {}
    message_count = 0
    tool_event_count = 0
    for session in SESSIONS.values():
        update_session_status(session)
        key = (session.get("status", "active"), session.get("outcome", "active"))
        outcome_counts[key] = outcome_counts.get(key, 0) + 1
        message_count += len(session.get("messages", []))
        tool_event_count += len(session.get("toolEvents", []))

    lines = [
        "# HELP chat_sessions_total Chat sessions by status and outcome.",
        "# TYPE chat_sessions_total gauge",
    ]
    for (status, outcome), count in sorted(outcome_counts.items()):
        lines.append(
            'chat_sessions_total{status="%s",outcome="%s"} %s'
            % (prometheus_label(status), prometheus_label(outcome), count)
        )
    lines.extend(
        [
            "# HELP chat_messages_total Messages retained in chat sessions.",
            "# TYPE chat_messages_total gauge",
            f"chat_messages_total {message_count}",
            "# HELP chat_tool_events_total Tool events retained in chat sessions.",
            "# TYPE chat_tool_events_total gauge",
            f"chat_tool_events_total {tool_event_count}",
            "# HELP chat_leads_total Leads retained by the chat backend.",
            "# TYPE chat_leads_total gauge",
            f"chat_leads_total {len(LEADS)}",
            "# HELP chat_bookings_total Bookings retained by the chat backend.",
            "# TYPE chat_bookings_total gauge",
            f"chat_bookings_total {len(BOOKINGS)}",
        ]
    )
    return "\n".join(lines) + "\n"


def build_system_prompt(tenant: TenantConfig) -> str:
    price_lines = "\n".join(
        f"- {service}: {price}" for service, price in tenant.get("prices", {}).items()
    )
    if not price_lines:
        price_lines = "- No prices are approved for chat."

    booking_line = (
        "Booking through chat is allowed. Always call get_availability before offering slots. "
        "Call book_appointment only after the user explicitly confirms a specific slot. "
        "When booking, pass the exact slot string returned by get_availability. "
        "A booking requires customer name, valid phone or email, and service address; "
        "the website may collect those with a structured form."
        if tenant["bookingEnabled"]
        else (
            "Booking through chat is not allowed. Do not call get_availability or "
            "book_appointment; tell users to call the company phone number."
        )
    )

    pricing_line = (
        "Pricing through chat is forbidden. Never provide prices, estimates, ranges, "
        "or guesses. For any pricing question, provide the phone number and say the "
        "team can help by phone. Offer to create a lead if the user wants a callback."
        if tenant["pricingPolicy"] == "never"
        else "Pricing through chat is allowed only for the approved prices listed below."
    )

    lead_line = (
        "Lead capture is enabled. Before calling create_lead, collect customer name, "
        "a valid email or complete 10 digit US phone number with area code, service, "
        "and a short request summary. Ask one concise follow-up for missing required "
        "fields. If the user gives a partial phone number, ask specifically for the "
        "area code or a complete phone number. Create a lead when the user asks for a "
        "callback, quote follow-up, human contact, or when booking/pricing policy "
        "prevents completing the request in chat. If create_lead returns invalid_contact, "
        "ask for a complete phone number with area code or an email."
        if tenant.get("leadCaptureEnabled")
        else "Lead capture is disabled. Do not call create_lead."
    )
    proactive_line = (
        "Proactive lead capture is enabled. When the user shows buying intent, asks about "
        "service area, pricing, availability, or seems likely to leave without booking, politely "
        "offer a callback or text follow-up. Do not pressure the user, do not imply consent, "
        "and do not say the company can call unless the user provides a phone or email."
        if tenant.get("proactiveLeadCapture")
        else "Proactive lead capture is disabled. Only ask for contact details when needed to complete the user's request."
    )

    return f"""
You are {tenant['assistantName']} for {tenant['name']}.

Business facts:
- Phone: {tenant['phone']}
- Address: {tenant['address']}
- Hours: {tenant['hours']}
- Services: {', '.join(tenant['services'])}

Policy:
- {pricing_line}
- {booking_line}
- {lead_line}
- {proactive_line}
- For service-area questions, call check_service_area with the ZIP code.
- If a user requests a human, asks for an exception, or needs something outside policy, call handoff_to_human.
- Keep replies concise, helpful, and specific to this company.

Approved prices:
{price_lines}
""".strip()


def call_llm(
    messages: List[Dict[str, Any]],
    tenant_id: str,
    session_id: str,
) -> Tuple[str, List[Dict[str, Any]]]:
    tenant = TENANTS[tenant_id]
    agent_messages: List[Dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt(tenant)}
    ]
    agent_messages.extend(messages[-16:])
    tool_events: List[Dict[str, Any]] = []

    for _ in range(MAX_TOOL_ROUNDS):
        response = post_openai_chat(
            {
                "model": LLM_MODEL,
                "messages": agent_messages,
                "tools": TOOLS,
                "tool_choice": "auto",
                "temperature": 0.2,
            }
        )
        choice = response["choices"][0]["message"]
        tool_calls = choice.get("tool_calls") or []

        if not tool_calls:
            return (choice.get("content") or "I can help with that.").strip(), tool_events

        agent_messages.append(choice)
        for call in tool_calls:
            name = call["function"]["name"]
            raw_args = call["function"].get("arguments") or "{}"
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
                args = {}

            result = execute_tool(tenant_id, session_id, tenant, name, args)
            tool_events.append({"name": name, "arguments": args, "result": result})
            agent_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(result),
                }
            )

    return (
        "I checked the available tools, but I need a person to finish this. "
        f"Please call {tenant['phone']}.",
        tool_events,
    )


def post_openai_chat(payload: Dict[str, Any]) -> Dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{LLM_BASE_URL.rstrip('/')}/chat/completions",
        data=body,
        headers=openai_request_headers(LLM_API_KEY),
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=LLM_TIMEOUT_SECONDS) as response:
        return cast(Dict[str, Any], json.loads(response.read().decode("utf-8")))


def execute_tool(
    tenant_id: str,
    session_id: str,
    tenant: TenantConfig,
    name: str,
    args: Dict[str, Any],
) -> Dict[str, Any]:
    if name == "check_service_area":
        zip_code = str(args.get("zip", "")).strip()
        return {
            "served": zip_code in tenant["serviceZips"],
            "zip": zip_code,
            "phone": tenant["phone"],
        }

    if name == "get_availability":
        if not tenant["bookingEnabled"]:
            return {"error": "booking_disabled", "phone": tenant["phone"]}
        service = normalize_service(tenant, str(args.get("service", "")))
        if not service:
            return {"error": "unknown_service", "allowed_services": tenant["services"]}
        return {"service": service, "slots": tenant["availability"].get(service, [])}

    if name == "book_appointment":
        if not tenant["bookingEnabled"]:
            return {"error": "booking_disabled", "phone": tenant["phone"]}
        service = normalize_service(tenant, str(args.get("service", "")))
        slot = str(args.get("slot", "")).strip()
        customer_name = clean_text(args.get("customer_name", ""))
        contact = normalize_contact(
            args.get("customer_phone_or_email", args.get("customer_phone", ""))
        )
        address = clean_text(args.get("address", ""))
        if not service:
            return {"error": "unknown_service", "allowed_services": tenant["services"]}
        missing_fields = [
            field
            for field, value in {
                "customer_name": customer_name,
                "customer_phone_or_email": contact,
                "address": address,
                "slot": slot,
            }.items()
            if not value
        ]
        if missing_fields:
            return {"error": "missing_required_fields", "missingFields": missing_fields}
        if not is_valid_contact(contact):
            return {
                "error": "invalid_contact",
                "field": "customer_phone_or_email",
                "message": "Provide a valid email or a complete 10 digit US phone number with area code.",
            }
        available_slots = tenant["availability"].get(service, [])
        matched_slot = find_matching_slot(slot, available_slots)
        if not matched_slot:
            return {
                "error": "slot_unavailable",
                "service": service,
                "slot": slot,
                "availableSlots": available_slots,
            }
        confirmation_id = f"BK-{int(time.time())}-{len(BOOKINGS) + 1}"
        booking = {
            "bookingId": confirmation_id,
            "tenantId": tenant_id,
            "tenantName": tenant["name"],
            "sessionId": session_id,
            "customerName": customer_name,
            "contact": contact,
            "address": address,
            "service": service,
            "slot": matched_slot,
            "createdAt": int(time.time()),
        }
        if not any(existing.get("bookingId") == confirmation_id for existing in BOOKINGS):
            BOOKINGS.append(booking)
        return {
            "status": "confirmed",
            "confirmationId": confirmation_id,
            "service": service,
            "slot": matched_slot,
            "customerName": customer_name,
            "contact": contact,
            "address": address,
        }

    if name == "create_lead":
        if not tenant.get("leadCaptureEnabled"):
            return {"error": "lead_capture_disabled", "phone": tenant["phone"]}

        customer_name = clean_text(args.get("customer_name", ""))
        contact = normalize_contact(args.get("customer_phone_or_email", ""))
        service = clean_text(args.get("service", ""))
        summary = clean_text(args.get("summary", ""))
        missing_fields = [
            field
            for field, value in {
                "customer_name": customer_name,
                "customer_phone_or_email": contact,
                "service": service,
                "summary": summary,
            }.items()
            if not value
        ]
        if missing_fields:
            return {"error": "missing_required_fields", "missingFields": missing_fields}
        if not is_valid_contact(contact):
            return {
                "error": "invalid_contact",
                "field": "customer_phone_or_email",
                "message": "Provide a valid email or a complete 10 digit US phone number with area code.",
            }

        lead = {
            "leadId": f"LD-{int(time.time())}-{len(LEADS) + 1}",
            "tenantId": tenant_id,
            "tenantName": tenant["name"],
            "sessionId": session_id,
            "customerName": customer_name,
            "contact": contact,
            "service": service,
            "addressOrZip": clean_text(args.get("address_or_zip", "")),
            "urgency": clean_text(args.get("urgency", "unknown")) or "unknown",
            "summary": summary,
            "createdAt": int(time.time()),
        }
        LEADS.append(lead)
        return {
            "status": "created",
            "leadId": lead["leadId"],
            "phone": tenant["phone"],
        }

    if name == "handoff_to_human":
        return {
            "status": "created",
            "handoffId": f"HO-{int(time.time())}",
            "phone": tenant["phone"],
            "reason": args.get("reason", "outside_policy"),
        }

    return {"error": "unknown_tool", "name": name}


def clean_text(value: Any) -> str:
    return str(value or "").strip()[:500]


def normalize_contact(value: Any) -> str:
    contact = clean_text(value)
    if "@" in contact:
        return contact
    digits = re.sub(r"\D", "", contact)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return contact


def is_valid_contact(contact: str) -> bool:
    if re.fullmatch(r"[\w.+-]+@[\w.-]+\.\w+", contact):
        return True
    digits = re.sub(r"\D", "", contact)
    return len(digits) == 10 or (len(digits) == 11 and digits.startswith("1"))


def latest_user_message(messages: List[Dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return str(message.get("content", ""))
    return ""


def is_financing_question(text: str) -> bool:
    return bool(
        re.search(
            r"\b(financ(?:e|ing)|payment(?:s| plan| option)?|monthly payment(?:s)?|"
            r"pay over time|apr|interest|loan|"
            r"credit|lender|deferred|installment|apply|approval|qualify|"
            r"promotional financing|same as cash)\b",
            text.lower(),
        )
    )


def call_financing_agent(
    tenant_id: str,
    session_id: str,
    messages: List[Dict[str, Any]],
) -> Tuple[str, List[Dict[str, Any]]]:
    if not FINANCING_AGENT_URL:
        raise urllib.error.URLError("FINANCING_AGENT_URL is not configured")
    query = latest_user_message(messages)
    payload = {
        "tenantId": tenant_id,
        "sessionId": session_id,
        "query": query,
        "messages": messages[-8:],
    }
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{FINANCING_AGENT_URL.rstrip('/')}/answer",
        data=body,
        headers={"Content-Type": "application/json", **internal_bearer_headers(FINANCING_AGENT_TOKEN)},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=LLM_TIMEOUT_SECONDS) as response:
        data = json.loads(response.read().decode("utf-8"))
    tool_event = {
        "name": "financing_agent",
        "arguments": {"query": query},
        "result": {
            "chunkCount": len(data.get("chunks", [])),
            "sources": [
                {
                    "title": chunk.get("title"),
                    "section": chunk.get("section"),
                    "score": chunk.get("score"),
                }
                for chunk in data.get("chunks", [])
            ],
        },
    }
    return data.get("answer", "I do not have enough financing information."), [tool_event]


def fallback_response(
    tenant_id: str,
    session_id: str,
    tenant: TenantConfig,
    messages: List[Dict[str, Any]],
) -> Tuple[str, List[Dict[str, Any]]]:
    """Rule fallback keeps the prototype usable if localhost:1234 is offline."""
    text = ""
    user_texts: List[str] = []
    for message in reversed(messages):
        if message.get("role") == "user":
            text = str(message.get("content", "")).lower()
            break
    for message in messages:
        if message.get("role") == "user":
            user_texts.append(str(message.get("content", "")))

    joined_user_text = "\n".join(user_texts)

    tool_events: List[Dict[str, Any]] = []
    if wants_lead(text):
        lead_args, missing_fields = extract_lead_args(tenant, joined_user_text)
        if missing_fields:
            return (
                "I can create a follow-up lead. Please send "
                + readable_missing_fields(missing_fields)
                + ".",
                tool_events,
            )
        result = execute_tool(tenant_id, session_id, tenant, "create_lead", lead_args)
        tool_events.append({"name": "create_lead", "arguments": lead_args, "result": result})
        return (
            f"I created lead {result['leadId']}. The {tenant['name']} team can follow up at "
            f"{lead_args['customer_phone_or_email']}.",
            tool_events,
        )

    zip_match = re.search(r"\b\d{5}\b", text)
    if zip_match:
        args = {"zip": zip_match.group(0)}
        result = execute_tool(tenant_id, session_id, tenant, "check_service_area", args)
        tool_events.append({"name": "check_service_area", "arguments": args, "result": result})
        if result["served"]:
            return (
                f"Yes, {tenant['name']} serves {args['zip']}. "
                + (
                    "Tell me which service category you need and I can check availability."
                    if tenant["bookingEnabled"]
                    else f"Please call {tenant['phone']} for the next step."
                ),
                tool_events,
            )
        return (
            f"{tenant['name']} does not currently show {args['zip']} in its service area. "
            f"Please call {tenant['phone']} to ask about exceptions.",
            tool_events,
        )

    if re.search(r"\b(hour|hours|open|close|closed|working)\b", text):
        return f"{tenant['name']} is open {tenant['hours']}.", tool_events

    if re.search(r"\b(phone|call|address|contact|where|location)\b", text):
        return (
            f"{tenant['name']} can be reached at {tenant['phone']}. "
            f"The address is {tenant['address']}.",
            tool_events,
        )

    if re.search(r"\b(price|pricing|cost|quote|estimate|rate|charge|how much)\b", text):
        if tenant["pricingPolicy"] == "never":
            return (
                f"{tenant['name']} does not provide pricing through chat. "
                f"Please call {tenant['phone']} and the team can help with an estimate. "
                "I can also create a follow-up lead if you send your name, phone or email, "
                "service, and address or ZIP.",
                tool_events,
            )
        lines = "\n".join(
            f"- {service.title()}: {price}" for service, price in tenant["prices"].items()
        )
        return f"Here are the approved prices I can share:\n{lines}", tool_events

    if re.search(r"\b(book|schedule|appointment|reserve|slot|available|availability)\b", text):
        if not tenant["bookingEnabled"]:
            return (
                f"{tenant['name']} does not allow booking through chat. "
                f"Please call {tenant['phone']} to schedule. I can also create a follow-up "
                "lead if you send your name, phone or email, service, and address or ZIP.",
                tool_events,
            )
        service = normalize_service(tenant, text)
        if not service:
            return (
                f"Which service category should I check: {', '.join(tenant['services'])}?",
                tool_events,
            )
        args = {"service": service}
        result = execute_tool(tenant_id, session_id, tenant, "get_availability", args)
        tool_events.append({"name": "get_availability", "arguments": args, "result": result})
        slots = result.get("slots", [])
        return (
            f"I found these {service.title()} openings:\n"
            + "\n".join(f"- {slot}" for slot in slots)
            + "\n\nTell me which slot you want and I can book it.",
            tool_events,
        )

    return (
        f"I can help with hours, contact info, service ZIP codes, "
        f"{'approved prices, appointment slots, and follow-up leads' if tenant['bookingEnabled'] else 'and creating a follow-up lead for the team'}.",
        tool_events,
    )


def wants_lead(text: str) -> bool:
    return bool(
        re.search(
            r"\b(call me|contact me|follow[- ]?up|lead|quote request|request a quote|"
            r"someone call|human|person|reach out|email me)\b",
            text,
        )
    )


def extract_lead_args(tenant: TenantConfig, text: str) -> Tuple[Dict[str, str], List[str]]:
    contact = extract_contact(text)
    customer_name = extract_name(text)
    service = normalize_service(tenant, text) or extract_service_description(text)
    zip_match = re.search(r"\b\d{5}\b", text)
    address_or_zip = zip_match.group(0) if zip_match else ""
    summary = text.strip()[:300]
    args = {
        "customer_name": customer_name,
        "customer_phone_or_email": contact,
        "service": service,
        "address_or_zip": address_or_zip,
        "urgency": extract_urgency(text),
        "summary": summary,
    }
    missing = [
        field
        for field in ["customer_name", "customer_phone_or_email", "service", "summary"]
        if not args[field]
    ]
    if contact and not is_valid_contact(contact):
        missing.append("valid_customer_phone_or_email")
    return args, missing


def extract_contact(text: str) -> str:
    email_match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", text)
    if email_match:
        return email_match.group(0)
    phone_match = re.search(r"(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}", text)
    if phone_match:
        return normalize_contact(phone_match.group(0))
    partial_phone_match = re.search(r"\b\d{3}[\s.-]?\d{4}\b", text)
    return partial_phone_match.group(0) if partial_phone_match else ""


def extract_name(text: str) -> str:
    name_match = re.search(
        r"\b(?:my name is|name is|i am|i'm)\s+([A-Za-z]+(?:\s+[A-Za-z]+){0,2})",
        text,
        re.IGNORECASE,
    )
    return name_match.group(1).strip() if name_match else ""


def extract_service_description(text: str) -> str:
    service_match = re.search(r"\b(?:for|need|about)\s+([A-Za-z][A-Za-z ]{2,40})", text)
    return service_match.group(1).strip() if service_match else ""


def extract_urgency(text: str) -> str:
    lowered = text.lower()
    if re.search(r"\b(emergency|urgent|asap|immediately)\b", lowered):
        return "emergency"
    if re.search(r"\b(today|tonight)\b", lowered):
        return "today"
    if re.search(r"\b(this week|week)\b", lowered):
        return "this_week"
    if re.search(r"\b(flexible|any time|whenever)\b", lowered):
        return "flexible"
    return "unknown"


def readable_missing_fields(fields: List[str]) -> str:
    labels = {
        "customer_name": "your name",
        "customer_phone_or_email": "a phone number or email",
        "valid_customer_phone_or_email": "a complete 10 digit phone number with area code or an email",
        "service": "the service you need",
        "summary": "a short note about the request",
    }
    return ", ".join(labels.get(field, field) for field in fields)


class ChatHandler(BaseHTTPRequestHandler):
    server_version = "TenantChatPrototype/0.1"

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/metrics":
            self.send_text(render_chat_metrics(), "text/plain; version=0.0.4")
            return

        if parsed.path == "/api/tenants":
            payload = {
                "tenants": {
                    tenant_id: public_tenant_config(config)
                    for tenant_id, config in TENANTS.items()
                }
            }
            self.send_json(payload)
            return

        if parsed.path == "/api/leads":
            params = urllib.parse.parse_qs(parsed.query)
            tenant_filter = params.get("tenantId", [None])[0]
            leads = [
                lead
                for lead in LEADS
                if tenant_filter is None or lead["tenantId"] == tenant_filter
            ]
            self.send_json({"leads": leads})
            return

        if parsed.path == "/api/admin/chats":
            sessions = sorted(
                (session_summary(session) for session in SESSIONS.values()),
                key=lambda item: item["updatedAt"],
                reverse=True,
            )
            self.send_json({"sessions": sessions})
            return

        if parsed.path.startswith("/api/admin/chats/"):
            session_id = urllib.parse.unquote(parsed.path.split("/")[-1])
            session = SESSIONS.get(session_id)
            if not session:
                self.send_json({"error": "session_not_found"}, status=404)
                return
            self.send_json({"session": session_detail(session)})
            return

        if parsed.path == "/api/chat/session":
            params = urllib.parse.parse_qs(parsed.query)
            session_id = params.get("sessionId", [""])[0]
            session = SESSIONS.get(session_id)
            if not session:
                self.send_json({"session": None})
                return
            self.send_json({"session": session_detail(session)})
            return

        path = "/index.html" if parsed.path == "/" else parsed.path
        self.serve_static(path)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/admin/chats/") and parsed.path.endswith("/messages"):
            self.handle_admin_message(parsed.path)
            return

        if parsed.path == "/api/book":
            self.handle_booking_form()
            return

        if parsed.path != "/api/chat":
            self.send_error(404)
            return

        try:
            request = self.read_json()
            tenant_id = request.get("tenantId", "apex")
            tenant = TENANTS[tenant_id]
            session_id = clean_text(request.get("sessionId", "")) or f"session-{int(time.time())}"
            messages = sanitize_messages(request.get("messages", []))
        except (KeyError, ValueError, json.JSONDecodeError):
            self.send_json({"error": "invalid_request"}, status=400)
            return

        session = ensure_session(tenant_id, session_id)
        set_session_messages(session, messages)

        try:
            if is_financing_question(latest_user_message(messages)):
                reply, tool_events = call_financing_agent(tenant_id, session_id, messages)
                mode = "financing_agent"
            else:
                reply, tool_events = call_llm(messages, tenant_id, session_id)
                mode = "llm"
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError, json.JSONDecodeError):
            reply, tool_events = fallback_response(tenant_id, session_id, tenant, messages)
            mode = "fallback"
            tool_events.append(
                {
                    "name": "llm_error",
                    "arguments": {},
                    "result": {"message": "The configured model dependency was unavailable."},
                }
            )

        append_tool_events(session, tool_events)
        append_session_message(session, "assistant", reply, "assistant")
        self.send_json({"reply": reply, "toolEvents": tool_events, "mode": mode})

    def handle_admin_message(self, path: str) -> None:
        session_id = urllib.parse.unquote(path.removeprefix("/api/admin/chats/").removesuffix("/messages"))
        session = SESSIONS.get(session_id)
        if not session:
            self.send_json({"error": "session_not_found"}, status=404)
            return
        try:
            request = self.read_json()
            content = clean_text(request.get("content", ""))
        except (ValueError, json.JSONDecodeError):
            self.send_json({"error": "invalid_request"}, status=400)
            return
        if not content:
            self.send_json({"error": "message_required"}, status=400)
            return
        message = append_session_message(session, "assistant", content, "admin")
        self.send_json({"message": message, "session": session_detail(session)})

    def handle_booking_form(self) -> None:
        try:
            request = self.read_json()
            tenant_id = request.get("tenantId", "apex")
            tenant = TENANTS[tenant_id]
            session_id = clean_text(request.get("sessionId", "")) or f"session-{int(time.time())}"
            session = ensure_session(tenant_id, session_id)
            args = {
                "service": request.get("service", ""),
                "slot": request.get("slot", ""),
                "customer_name": request.get("customerName", ""),
                "customer_phone_or_email": request.get("contact", ""),
                "address": request.get("address", ""),
            }
        except (KeyError, ValueError, json.JSONDecodeError):
            self.send_json({"error": "invalid_request"}, status=400)
            return

        result = execute_tool(tenant_id, session_id, tenant, "book_appointment", args)
        append_session_message(
            session,
            "user",
            (
                f"Booking form submitted for {clean_text(args['service'])}: "
                f"{clean_text(args['slot'])}. Name: {clean_text(args['customer_name'])}. "
                f"Contact: {clean_text(args['customer_phone_or_email'])}. "
                f"Address: {clean_text(args['address'])}."
            ),
            "user",
        )
        tool_event = {"name": "book_appointment", "arguments": args, "result": result}
        append_tool_events(session, [tool_event])

        if result.get("status") == "confirmed":
            reply = (
                f"Booked {result['service']} for {result['slot']}. "
                f"Confirmation {result['confirmationId']}."
            )
            append_session_message(session, "assistant", reply, "assistant")
            self.send_json({"reply": reply, "toolEvent": tool_event, "session": session_detail(session)})
            return

        self.send_json({"error": "booking_failed", "toolEvent": tool_event}, status=400)

    def serve_static(self, path: str) -> None:
        clean_path = urllib.parse.unquote(path).lstrip("/")
        file_path = (STATIC_ROOT / clean_path).resolve()
        if STATIC_ROOT not in file_path.parents and file_path != STATIC_ROOT:
            self.send_error(403)
            return
        if not file_path.is_file():
            self.send_error(404)
            return
        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(file_path.read_bytes())

    def read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        return cast(Dict[str, Any], json.loads(self.rfile.read(length).decode("utf-8")))

    def send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, body: str, content_type: str, status: int = 200) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_cors_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def log_message(self, format: str, *args: Any) -> None:
        print("%s - %s" % (self.address_string(), format % args))


_PUBLIC_GET_PATHS = frozenset(
    {
        "/",
        "/index.html",
        "/app.js",
        "/embed.js",
        "/styles.css",
        "/widget/api.js",
        "/widget/embed.js",
        "/widget/privacy.js",
        "/widget/styles.js",
        "/widget/widget.js",
        "/api/tenants",
        "/api/chat/session",
    }
)
_PUBLIC_POST_PATHS = frozenset({"/api/chat", "/api/book"})


def is_public_route(method: str, path: str) -> bool:
    """Return whether a route belongs on the internet-facing listener."""
    normalized_path = urllib.parse.urlparse(path).path
    if method in {"GET", "HEAD"}:
        return normalized_path in _PUBLIC_GET_PATHS
    if method in {"POST", "OPTIONS"}:
        return normalized_path in _PUBLIC_POST_PATHS
    return False


class PublicChatHandler(ChatHandler):
    """Expose visitor routes only; admin and metrics stay on the internal port."""

    def _reject_non_public(self) -> bool:
        if is_public_route(self.command, self.path):
            return False
        self.send_error(404)
        return True

    def do_GET(self) -> None:
        if not self._reject_non_public():
            super().do_GET()

    def do_POST(self) -> None:
        if not self._reject_non_public():
            super().do_POST()

    def do_OPTIONS(self) -> None:
        if not self._reject_non_public():
            super().do_OPTIONS()


def sanitize_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    cleaned = []
    for message in messages[-24:]:
        role = message.get("role")
        content = message.get("content")
        source = message.get("source")
        if role in {"user", "assistant"} and isinstance(content, str):
            cleaned.append(
                {
                    "role": role,
                    "content": content[:4000],
                    "source": source if source in {"user", "assistant", "admin", "proactive"} else role,
                }
            )
    return cleaned


def main() -> None:
    load_saved_chats()
    handler = ChatHandler
    if is_production():
        internal_server = ThreadingHTTPServer((HOST, ADMIN_PORT), ChatHandler)
        internal_thread = threading.Thread(target=internal_server.serve_forever, daemon=True)
        internal_thread.start()
        print(f"Serving internal chat administration at http://{HOST}:{ADMIN_PORT}")
        handler = PublicChatHandler
    server = ThreadingHTTPServer((HOST, PORT), handler)
    print(f"Serving tenant chat prototype at http://{HOST}:{PORT}")
    print(f"Persisting chat archives in {storage_description()}")
    print("OpenAI-compatible chat provider configured")
    server.serve_forever()


if __name__ == "__main__":
    main()
