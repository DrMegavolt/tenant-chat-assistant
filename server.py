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
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


ROOT = Path(__file__).resolve().parent
HOST = os.environ.get("CHAT_HOST", "127.0.0.1")
PORT = int(os.environ.get("CHAT_PORT", "8000"))
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://localhost:1234/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "local-model")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
MAX_TOOL_ROUNDS = 4


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
        "services": ["HVAC", "Electrical", "Plumbing"],
        "serviceZips": ["98101", "98102", "98103", "98104", "98105"],
        "prices": {},
        "availability": {},
        "quickActions": [
            "What are your hours?",
            "Do you serve 98103?",
            "How much is HVAC repair?",
            "Can I book electrical?",
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
                    "customer_phone": {"type": "string"},
                },
                "required": ["service", "slot"],
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
            return canonical
    return None


def build_system_prompt(tenant: TenantConfig) -> str:
    price_lines = "\n".join(
        f"- {service}: {price}" for service, price in tenant.get("prices", {}).items()
    )
    if not price_lines:
        price_lines = "- No prices are approved for chat."

    booking_line = (
        "Booking through chat is allowed. Always call get_availability before offering slots. "
        "Call book_appointment only after the user explicitly confirms a specific slot."
        if tenant["bookingEnabled"]
        else (
            "Booking through chat is not allowed. Do not call get_availability or "
            "book_appointment; tell users to call the company phone number."
        )
    )

    pricing_line = (
        "Pricing through chat is forbidden. Never provide prices, estimates, ranges, "
        "or guesses. For any pricing question, provide the phone number and say the "
        "team can help by phone."
        if tenant["pricingPolicy"] == "never"
        else "Pricing through chat is allowed only for the approved prices listed below."
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
- For service-area questions, call check_service_area with the ZIP code.
- If a user requests a human, asks for an exception, or needs something outside policy, call handoff_to_human.
- Keep replies concise, helpful, and specific to this company.

Approved prices:
{price_lines}
""".strip()


def call_llm(messages: List[Dict[str, Any]], tenant: TenantConfig) -> Tuple[str, List[Dict[str, Any]]]:
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

            result = execute_tool(tenant, name, args)
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
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {LLM_API_KEY}"} if LLM_API_KEY else {}),
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.loads(response.read().decode("utf-8"))


def execute_tool(tenant: TenantConfig, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
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
        if not service:
            return {"error": "unknown_service", "allowed_services": tenant["services"]}
        if slot not in tenant["availability"].get(service, []):
            return {"error": "slot_unavailable", "service": service, "slot": slot}
        confirmation_id = f"BK-{int(time.time())}"
        return {
            "status": "confirmed",
            "confirmationId": confirmation_id,
            "service": service,
            "slot": slot,
        }

    if name == "handoff_to_human":
        return {
            "status": "created",
            "handoffId": f"HO-{int(time.time())}",
            "phone": tenant["phone"],
            "reason": args.get("reason", "outside_policy"),
        }

    return {"error": "unknown_tool", "name": name}


def fallback_response(
    tenant: TenantConfig, messages: List[Dict[str, Any]]
) -> Tuple[str, List[Dict[str, Any]]]:
    """Rule fallback keeps the prototype usable if localhost:1234 is offline."""
    text = ""
    for message in reversed(messages):
        if message.get("role") == "user":
            text = str(message.get("content", "")).lower()
            break

    tool_events: List[Dict[str, Any]] = []
    zip_match = re.search(r"\b\d{5}\b", text)
    if zip_match:
        args = {"zip": zip_match.group(0)}
        result = execute_tool(tenant, "check_service_area", args)
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
                f"Please call {tenant['phone']} and the team can help with an estimate.",
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
                f"Please call {tenant['phone']} to schedule.",
                tool_events,
            )
        service = normalize_service(tenant, text)
        if not service:
            return (
                f"Which service category should I check: {', '.join(tenant['services'])}?",
                tool_events,
            )
        args = {"service": service}
        result = execute_tool(tenant, "get_availability", args)
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
        f"{'approved prices, and appointment slots' if tenant['bookingEnabled'] else 'and routing you to the team'}.",
        tool_events,
    )


class ChatHandler(BaseHTTPRequestHandler):
    server_version = "TenantChatPrototype/0.1"

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/tenants":
            payload = {
                "tenants": {
                    tenant_id: public_tenant_config(config)
                    for tenant_id, config in TENANTS.items()
                }
            }
            self.send_json(payload)
            return

        path = "/index.html" if parsed.path == "/" else parsed.path
        self.serve_static(path)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/chat":
            self.send_error(404)
            return

        try:
            request = self.read_json()
            tenant_id = request.get("tenantId", "apex")
            tenant = TENANTS[tenant_id]
            messages = sanitize_messages(request.get("messages", []))
        except (KeyError, ValueError, json.JSONDecodeError):
            self.send_json({"error": "invalid_request"}, status=400)
            return

        try:
            reply, tool_events = call_llm(messages, tenant)
            mode = "llm"
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError, json.JSONDecodeError) as error:
            reply, tool_events = fallback_response(tenant, messages)
            mode = "fallback"
            tool_events.append(
                {
                    "name": "llm_error",
                    "arguments": {"baseUrl": LLM_BASE_URL, "model": LLM_MODEL},
                    "result": {"message": str(error)},
                }
            )

        self.send_json({"reply": reply, "toolEvents": tool_events, "mode": mode})

    def serve_static(self, path: str) -> None:
        clean_path = urllib.parse.unquote(path).lstrip("/")
        file_path = (ROOT / clean_path).resolve()
        if ROOT not in file_path.parents and file_path != ROOT:
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
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def log_message(self, format: str, *args: Any) -> None:
        print("%s - %s" % (self.address_string(), format % args))


def sanitize_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    cleaned = []
    for message in messages[-24:]:
        role = message.get("role")
        content = message.get("content")
        if role in {"user", "assistant"} and isinstance(content, str):
            cleaned.append({"role": role, "content": content[:4000]})
    return cleaned


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), ChatHandler)
    print(f"Serving tenant chat prototype at http://{HOST}:{PORT}")
    print(f"Calling OpenAI-compatible chat API at {LLM_BASE_URL}/chat/completions")
    server.serve_forever()


if __name__ == "__main__":
    main()
