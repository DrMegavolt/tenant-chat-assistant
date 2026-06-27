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

The backend owns the tenant policy and tool calls:

- Tenant configuration: allowed services, pricing policy, booking policy, phone, address, hours, escalation rules.
- Retrieval: approved company knowledge base for FAQs.
- Tools: `checkServiceArea`, `getAvailability`, `bookAppointment`, `createLead`, `handoffToHuman`.
- Guardrails: no pricing unless policy allows it, no booking unless policy allows it, human handoff for uncertainty or risky requests.

Example embed shape:

```html
<script
  src="https://your-domain.com/chat-widget.js"
  data-company-id="clearview"
></script>
```
