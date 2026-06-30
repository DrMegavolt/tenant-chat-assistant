# Tenant Chat Assistant Prototype

This is a prototype for an embeddable website chat widget with a Python backend.

Run it locally:

```bash
python3 server.py
```

Then open:

```text
http://127.0.0.1:8000
```

Admin dashboard:

```text
http://127.0.0.1:8000/admin.html
```

Chat archives are saved as JSON files in:

```text
chats/
```

By default, the backend calls an OpenAI-compatible local API at:

```text
http://localhost:1234/v1/chat/completions
```

You can override this with environment variables:

```bash
LLM_BASE_URL=http://localhost:1234/v1 LLM_MODEL=local-model python3 server.py
```

Switch between the two configured companies:

- Company A: answers contact, address, hours, and service-area questions, but never shares pricing and does not book through chat.
- Company B: answers from fixed pricing, checks ZIP-code service area, separates availability by service category, and books a selected slot after confirmation.
- Both companies can capture follow-up leads after collecting name, contact, service, and request details.
- Both companies can politely offer callback capture after buying intent, but the assistant should not imply it can call unless the visitor provides contact info.

The backend owns the tenant policy and tool calls:

- Tenant configuration: allowed services, pricing policy, booking policy, phone, address, hours, escalation rules.
- Retrieval: approved company knowledge base for FAQs.
- Tools: `check_service_area`, `get_availability`, `book_appointment`, `create_lead`, `handoff_to_human`.
- Guardrails: no pricing unless policy allows it, no booking unless policy allows it, human handoff for uncertainty or risky requests.
- Admin: live chat list, transcript view, lead/tool panels, and manual staff replies into a visitor chat.
- Persistence: each session is saved to `chats/<session-id>.json` and loaded again when the server starts.
- Outcomes: admin marks chats as active, abandoned, booked, lead, handoff, completed, or empty.

Inspect captured prototype leads:

```text
http://127.0.0.1:8000/api/leads
```

Useful lead-capture test message:

```text
Please have someone call me. My name is Sam Lee, my phone is 555-222-1919, I need HVAC help in 97205 this week.
```

Example embed shape:

```html
<script
  src="https://your-domain.com/chat-widget.js"
  data-company-id="clearview"
></script>
```
