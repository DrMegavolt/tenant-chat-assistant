# 0006 — nginx serves the frontend

- **Status:** Superseded by [ADR-0007](0007-single-origin-gateway.md)
- **Date:** 2026-08-02

## Context

The original Python server also served static browser assets. That mixed cache,
compression, and browser security policy with API code and made frontend changes
depend on the backend process.

## Decision

Introduce an nginx web image that owns frontend assets, security headers, and
API proxying. Keep the backend API-only. The initial design used separate public
and admin listeners so admin assets and routes were not exposed publicly.

## Outcome

The ownership decision remains: nginx still serves the browser application and
the backend still serves only APIs. The two-listener isolation model was replaced
by ADR-0007 because separate browser origins complicated authentication and
admin API access.

This record is retained only to explain that transition.
