# 0009 — React and TypeScript behind a bundled build, with the embed as a published artefact

- **Status:** Accepted
- **Date:** 2026-08-02
- **Affects:** `ADR-0006`, `ADR-0007`, `DEP-003`, `FEAT-013`

## Context

The browser code was hand-written DOM: `frontend/public/widget/widget.js` built
every element with a `createElement` helper, tracked open/closed and waiting
state on a class instance, and re-derived what to show from that state at each
call site. The operator console went further and re-rendered its entire document
every two seconds, then restored scroll offsets, focus, the reply field's value,
and the caret position afterwards to hide the damage. That restore function was
not an optimisation; it was the console's only defence against the render
strategy underneath it.

Both are the same problem. When the view is produced imperatively, every new
piece of state has to be threaded by hand into every place that displays it, and
the cost of getting it wrong is a UI that disagrees with itself. Adding a
per-message timestamp, an unread count, or a second panel meant adding another
manual update path.

There was also no build step, deliberately: nginx and the Vite dev server both
served the same unbundled ES modules. That bought byte-for-byte parity between
development and production at the cost of no type checking, no dependency on
anything from npm, and — because `widget/embed.js` imported four sibling modules
— an embed that could not actually be loaded cross-origin, since module imports
are fetched under CORS and only `/api/` paths carried allowlisted headers.

## Decision

Rewrite the browser code as React 19 with TypeScript in `strict` mode, built by
Vite. State is declared once and rendered from; `ChatWidget` is keyed by tenant
id, so switching tenants replaces the component rather than resetting it field
by field, and the console's restore-the-caret function is deleted rather than
ported — a poll now updates data, and anything an operator is in the middle of
is component state that polling never touches.

The widget still renders into a shadow root. React reaches it two ways: the demo
page portals into it, which keeps the widget inside the host application's tree,
and `mountWidget` creates a root on it directly for plain-HTML embedders.

Three build passes, not one:

| Pass | Output | Why separate |
| --- | --- | --- |
| `vite build` | `dist/public` | The internet-facing document root |
| `vite build --mode embed` | `dist/public/embed.js` | One self-contained, unhashed file |
| `vite build --mode admin` | `dist/admin`, base `/admin/` | The auth-gated document root |

Public and admin are separate builds because ADR-0007's isolation argument is
that the two document roots share nothing. A single build emits shared hashed
chunks that both roots need, and the only way to serve them is to publish admin
code from the public root.

The embed is separate for a different reason. `embed.js` is a URL pasted into
other people's HTML: it may not be hashed, and its imports may not be split,
because each chunk would be a further cross-origin fetch. It builds to one file
with code splitting off, and the gateway answers `/embed.js` with the same
allowlisted CORS map the visitor API uses.

`frontend/Dockerfile` gains a digest-pinned Node build stage running `npm ci`, so
the bundles come from `package-lock.json` and nothing else — the same guarantee
`uv sync --frozen` gives the Python images.

## Consequences

Asset filenames now carry content hashes, so `/assets/` is served
`max-age=31536000, immutable`. This closes the caching half of `DEP-003` that
ADR-0006 left open. `embed.js` still revalidates, because its URL is fixed.

The dev server and the image no longer serve identical bytes. What they do share
is the module graph and the toolchain: the same Vite resolves the same imports,
and `make check` builds all three bundles, so a build-only failure cannot reach
a release.

Type checking is a gate (`make js-typecheck`, mypy's counterpart), and the widget
contract is expressed in types rather than in comments — `TranscriptEntry` is a
discriminated union of the three things a transcript can hold, so a new kind of
entry cannot be added without handling it where the transcript renders.

The accessibility and privacy suites were ported, not rewritten: they assert the
same guarantees against the same element ids, so the refactor is checked against
the behaviour it replaced. The contrast suite gained a scheme axis — the widget
now honours `prefers-color-scheme`, and every pair it paints is proven against
WCAG 2.2 AA in both.

React and React DOM are the first runtime dependencies the browser bundle has
ever had; the widget is ~68 kB gzipped where it was a few kB of hand-written
DOM. That is the price of the state model, and it is paid by the visitor.

`style-src 'unsafe-inline'` is still required: the shadow-root stylesheet is
still written as a `<style>` element, now from a CSS file imported with Vite's
`?inline`. Constructed stylesheets remain the way to remove it.

## Alternatives considered

**Keep the hand-written DOM and fix the console's polling.** Cheapest, and it
would have removed the restore-the-caret function. It leaves every future piece
of state to be threaded by hand, which is the actual defect, and it does not give
the widget a type-checked contract.

**A smaller runtime — Preact, Lit, or Solid.** Preact would cut roughly 40 kB
gzipped and is a real candidate for an embed. Rejected for now because the
console is the larger surface and benefits from the mainstream ecosystem, and
because running two frameworks to save bytes on one of them is worse than
running one. If the embed's size becomes a constraint, `preact/compat` is a
build-level swap.

**One build with manual chunking rules.** Keeps a single pass, and Rollup can be
told which module belongs to which chunk. Rejected because the isolation between
the roots would then depend on a chunking rule staying correct, rather than on
two builds that cannot share a file. A config mistake is not supposed to be able
to publish the console.

**No bundler; ship TypeScript compiled to plain modules.** Preserves the
unbundled shape and adds types. It does not fix the cross-origin embed, since
the module graph is still fetched file by file, and it gives up hashed filenames
and therefore immutable caching.
