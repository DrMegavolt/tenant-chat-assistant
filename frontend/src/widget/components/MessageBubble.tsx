import type { MessageRole, MessageSource } from "src/widget/types";

export interface MessageBubbleProps {
  role: MessageRole;
  source: MessageSource;
  text: string;
}

/**
 * Model copy sometimes arrives with a space ahead of sentence punctuation
 * ("Hello , world !"). Tidying it in the transcript state instead would
 * corrupt the exact text the transcript merge matches on and the copy the
 * server stored, so this runs only at render.
 *
 * The rule is deliberately narrow: whitespace immediately before
 * `.`, `!`, `?`, `;`, `:` counts only when the punctuation is itself followed
 * by whitespace, a quote, or the end of the text. Punctuation inside a URL or
 * a value like `a.b` has no whitespace before it and is never touched.
 */
const STRAY_SPACE_BEFORE_PUNCTUATION = /\s+([.,!?;:])(?=\s|$|["'”’])/g;
const DOUBLED_SPACES = / {2,}/g;

/**
 * A citation marker the server did not deliver as a citation. The backend
 * strips well-formed ``[evidence:<source_id>]`` markers from every published
 * answer, but a malformed one the model wrote (a space after the colon, spaces
 * in the label — "[evidence: business facts]") matches neither the evidence
 * catalog nor the strip rule and reaches the visitor as raw markup. The source
 * chips render from the turn's validated citation list, never from the text, so
 * removing the tag here cannot lose a reference; the transcript state keeps the
 * raw text for the merge, the poll, and the server record.
 */
const UNDELIVERED_EVIDENCE_TAG = /\s*\[evidence:\s*[^\]]{0,200}\]/g;

export function displayCopy(text: string): string {
  return text
    .replace(UNDELIVERED_EVIDENCE_TAG, "")
    .replace(STRAY_SPACE_BEFORE_PUNCTUATION, "$1")
    .replace(DOUBLED_SPACES, " ")
    .trim();
}

/** The name a listener hears in place of the alignment a reader sees. */
export function speakerLabel(role: MessageRole, source: MessageSource): string {
  if (source === "admin") return "Staff";
  return role === "user" ? "You" : "Assistant";
}

export function MessageBubble({ role, source, text }: MessageBubbleProps) {
  const label = speakerLabel(role, source);
  const variant = source === "user" || source === "assistant" ? "" : ` ${source}`;
  return (
    <div className={`message ${role}${variant}`}>
      <span className="visually-hidden">{`${label}: `}</span>
      {source === "admin" && (
        // Visual duplicate of the label above, which is already announced.
        <span className="message-attribution" aria-hidden="true">
          {label}
        </span>
      )}
      <span>{displayCopy(text)}</span>
    </div>
  );
}
