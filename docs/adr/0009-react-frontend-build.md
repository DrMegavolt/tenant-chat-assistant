# 0009 — React build with a self-contained embed

- **Status:** Accepted
- **Date:** 2026-08-02

## Context

The original browser code managed DOM and polling state manually. The widget's
module graph also required several cross-origin fetches, so a customer could not
reliably load it from one script URL.

## Decision

Use React with strict TypeScript and build it with Vite. Produce three outputs:

1. The public site under `dist/public`.
2. A single, unhashed `dist/public/embed.js` with code splitting disabled.
3. The operator console under `dist/admin` with `/admin/` as its base.

The public and admin builds stay separate so the public document root cannot
contain shared chunks with admin code. The embed stays self-contained because it
is loaded cross-origin from customer HTML and must work from one stable URL.

The container builds assets with `npm ci` from the committed lockfile. Hashed
assets receive immutable caching; HTML and `embed.js` revalidate.

## Consequences

UI state has a typed, declarative model, and all three production artifacts are
built during repository checks. The visitor embed can be published as one file
without exposing admin assets.

The production bytes differ from the development server, and React increases
the widget size. The shadow-root stylesheet still requires an inline-style CSP
allowance.

## Alternatives considered

- **Keep the hand-written DOM:** smaller, but leaves state synchronization and
  type safety as manual responsibilities.
- **Use a smaller UI runtime:** worth revisiting if embed size becomes a product
  constraint.
- **Use one build with manual chunks:** rejected because isolation would depend
  on a fragile bundler rule.
