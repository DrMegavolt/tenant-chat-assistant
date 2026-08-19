# 0004 — Local OpenAI-compatible model provider by default

- **Status:** Accepted
- **Date:** 2026-07-31

## Context

Model and retrieval evaluations run frequently during development. Requiring a
hosted API for every run adds cost and makes the repository depend on an
external account. A public deployment, however, cannot reach a model running on
a developer's machine.

## Decision

Default to a local OpenAI-compatible endpoint and keep provider details behind
the orchestration model port.

- Base URL and model selection come from configuration, never visitor input.
- The adapter normalizes completion, tool-call, usage, and error behavior.
- Response streaming is outside the current provider contract.
- Hosted OpenAI-compatible providers can be selected without changing workflow
  or domain code.
- The embedding service uses a pinned model revision with remote model code
  disabled.

## Consequences

The project runs and evaluates without an API key, and local failure modes are
exercised routinely. Provider changes remain configuration and adapter concerns.

There is no public demo backed by a developer's local endpoint. Local models may
also differ from hosted models in tool use and instruction following, so every
configured model must pass the same evaluation contract before use.

## Alternatives considered

- **Hosted provider by default:** rejected because recurring evaluation cost
  would discourage routine testing.
- **Hosted production and local development:** compatible with this decision
  and appropriate when a hosted deployment is needed.
- **Several provider-specific adapters immediately:** deferred until a concrete
  provider requires behavior beyond the compatible interface.
