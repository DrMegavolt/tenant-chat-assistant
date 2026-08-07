import type { Citation } from "src/widget/types";

export interface CitationListProps {
  citations: Citation[];
  /** Open the source viewer; the triggering button is reported for focus return. */
  onOpen: (citation: Citation, trigger: HTMLButtonElement) => void;
}

/**
 * The compact, numbered list of sources one answer draws on.
 *
 * Each entry is a real button: a keyboard visitor can Tab to it and open it
 * with Enter or Space, which is how the source viewer stays reachable without a
 * mouse. The number badge is decoration; the accessible name carries the
 * ordinal so a screen-reader user hears "Source 2: <title>".
 */
export function CitationList({ citations, onOpen }: CitationListProps) {
  return (
    <div className="citation-list" role="group" aria-label="Sources this answer draws on">
      <ol>
        {citations.map((citation, index) => (
          <li key={citation.sourceId}>
            <button
              type="button"
              className="citation-button"
              aria-label={`Source ${index + 1}: ${citation.title}`}
              onClick={(event) => onOpen(citation, event.currentTarget)}
            >
              <span className="citation-badge" aria-hidden="true">
                {index + 1}
              </span>
              <span className="citation-title">{citation.title}</span>
            </button>
          </li>
        ))}
      </ol>
    </div>
  );
}
