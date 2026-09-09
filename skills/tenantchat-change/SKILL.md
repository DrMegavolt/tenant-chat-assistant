---
name: tenantchat-change
description: Implement and verify Tenant Chat feature changes or reliability fixes from executable acceptance criteria. Use for changes to this repository's API, domain behavior, orchestration, or persistence.
---

Read the root CLAUDE.md and the relevant ADR.
docs/specs/knowledge-publication.md is a worked example of a spec-to-code change.

Before changing behavior, record the trigger, expected result, tenant boundary,
failure behavior, and exclusions in a focused spec. Reuse an existing spec when
it covers the change. For HTTP changes, describe fields, errors, and compatibility
before changing models; contracts/openapi.json is the reviewed schema baseline.

Write a regression that fails on the old behavior. Transactions, unique-key
races, and restart guarantees require the PostgreSQL suites, not only in-memory
fakes. Agent text is untrusted; authorization and effects stay behind domain
ports. Inspect the smallest existing implementation before adding an abstraction.

Run the relevant targeted checks, then make check. Run make test-database for
persistence or transaction changes. Keep required assertions and thresholds;
resolve ambiguity in the spec rather than weakening a gate. Report failures and
skips accurately. Do not assert that a scripted model test proves live quality.

Summarize the spec-to-code mapping, actual verification, and remaining risks in
the PR template. Human review of intent and tradeoffs remains outstanding until
a person performs it; the agent may prepare all review materials.
