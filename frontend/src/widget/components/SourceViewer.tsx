import { useEffect, useRef, useState, type KeyboardEvent } from "react";

import { CloseIcon } from "src/widget/icons";
import type { Citation, SourceView } from "src/widget/types";

export interface SourceViewerProps {
  citation: Citation;
  /** Fetch the authorized source view for the conversation's credential. */
  load: (sourceId: string) => Promise<SourceView>;
  onClose: () => void;
}

const FOCUSABLE_SELECTOR =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** The date half of an ISO instant, kept local-zone-free so it reads the same
 * everywhere the widget renders. */
function effectiveDate(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toISOString().slice(0, 10);
}

/**
 * The authorized view of one cited source.
 *
 * The widget treats the source as a question to the server, never as a fact it
 * already holds: the backend rechecks tenant, audience, and quarantine on every
 * read, and whatever it refuses to answer degrades to a single bounded line —
 * no status, no detail, no hint of whether the source ever existed.
 *
 * Keyboard behaviour is the same modal contract as the panel: Escape closes and
 * focus is trapped inside, returning to the citation button that opened it.
 */
export function SourceViewer({ citation, load, onClose }: SourceViewerProps) {
  const [view, setView] = useState<SourceView | null>(null);
  const [failed, setFailed] = useState(false);
  const [loading, setLoading] = useState(true);
  const dialogRef = useRef<HTMLDivElement>(null);

  // The viewer is mounted per citation, so the initial state is already the
  // pre-fetch one; only the async completion may set state again.
  useEffect(() => {
    let cancelled = false;
    load(citation.sourceId)
      .then((loaded) => {
        if (!cancelled) setView(loaded);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [citation.sourceId, load]);

  useEffect(() => {
    dialogRef.current?.focus();
  }, []);

  const trapFocus = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      // The panel listens for Escape on its wrapper; a closing modal must not
      // also close the whole widget.
      event.stopPropagation();
      onClose();
      return;
    }
    if (event.key !== "Tab") return;
    const dialog = dialogRef.current;
    if (!dialog) return;
    const focusables = [...dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)];
    if (focusables.length === 0) return;
    const first = focusables[0]!;
    const last = focusables[focusables.length - 1]!;
    const root = dialog.getRootNode();
    const active =
      root instanceof ShadowRoot ? root.activeElement : (root as Document).activeElement;
    if (event.shiftKey && (active === first || !dialog.contains(active))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (active === last || !dialog.contains(active))) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <div
      ref={dialogRef}
      className="source-viewer"
      role="dialog"
      aria-modal="true"
      aria-labelledby="sourceViewerTitle"
      tabIndex={-1}
      onKeyDown={trapFocus}
    >
      {/* A `header` here would register a second banner landmark; a dialog's
          chrome is presentation, not navigation. */}
      <div className="source-viewer-header">
        <h2 id="sourceViewerTitle">{view?.title ?? citation.title}</h2>
        <button
          type="button"
          className="icon-button source-viewer-close"
          id="sourceViewerClose"
          onClick={onClose}
        >
          <CloseIcon />
          <span className="visually-hidden">Close source</span>
        </button>
      </div>

      <div className="source-viewer-body">
        {loading && (
          <p className="source-viewer-status" role="status">
            Loading source…
          </p>
        )}
        {failed && (
          <p className="source-viewer-status error" role="alert">
            This source is no longer available to you.
          </p>
        )}
        {view && (
          <>
            <dl className="source-viewer-meta">
              <div>
                <dt>Publication</dt>
                <dd>{view.sourceName}</dd>
              </div>
              <div>
                <dt>Section</dt>
                <dd>{view.location}</dd>
              </div>
              <div>
                <dt>Version</dt>
                <dd>
                  Revision {view.revision} · effective {effectiveDate(view.effectiveAt)}
                </dd>
              </div>
            </dl>
            <blockquote className="source-viewer-excerpt">{view.text}</blockquote>
          </>
        )}
      </div>
    </div>
  );
}
