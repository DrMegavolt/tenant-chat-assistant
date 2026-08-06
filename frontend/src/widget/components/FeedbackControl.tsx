import { useId, useState } from "react";

export interface FeedbackControlProps {
  /** The rating already given, if any. */
  rating: "up" | "down" | null;
  onRate: (rating: "up" | "down", reason?: string) => Promise<void>;
}

const MAX_REASON_LENGTH = 1000;

/**
 * The visitor's rating of one assistant answer (`FEAT-008`).
 *
 * Two accessible toggle buttons (thumbs up / down); a thumbs-down reveals the
 * optional reason field and a submit control. Once a rating is recorded the
 * control collapses into a confirmation announced through a status region —
 * never through a change of focus, which would steal the visitor's place in
 * the transcript.
 */
export function FeedbackControl({ rating, onRate }: FeedbackControlProps) {
  const labelId = useId();
  const [picking, setPicking] = useState<"up" | "down" | null>(null);
  const [reason, setReason] = useState("");
  const [saving, setSaving] = useState(false);

  if (rating !== null) {
    return (
      <p className="feedback-confirmed" role="status">
        {rating === "up"
          ? "Thanks — glad this helped."
          : "Thanks — the team will review this answer."}
      </p>
    );
  }

  const submit = async (choice: "up" | "down") => {
    if (choice === "up") {
      setSaving(true);
      await onRate("up");
      return;
    }
    if (picking === null) {
      setPicking("down");
      return;
    }
    setSaving(true);
    await onRate("down", reason.trim() || undefined);
  };

  return (
    <div className="feedback-control">
      <span className="visually-hidden" id={labelId}>
        Was this answer helpful?
      </span>
      <div role="group" aria-labelledby={labelId}>
        <button
          type="button"
          className="feedback-button"
          aria-pressed={picking === "up"}
          disabled={saving}
          onClick={() => void submit("up")}
        >
          Thumbs up
        </button>
        <button
          type="button"
          className="feedback-button"
          aria-pressed={picking === "down"}
          disabled={saving}
          onClick={() => void submit("down")}
        >
          Thumbs down
        </button>
      </div>
      {picking === "down" && (
        <form
          className="feedback-reason"
          onSubmit={(event) => {
            event.preventDefault();
            void submit("down");
          }}
        >
          <label htmlFor={labelId}>
            <span className="visually-hidden">What went wrong? (optional)</span>
            <textarea
              id={labelId}
              value={reason}
              maxLength={MAX_REASON_LENGTH}
              placeholder="What went wrong? (optional)"
              disabled={saving}
              onChange={(event) => setReason(event.target.value)}
            />
          </label>
          <button type="submit" className="feedback-button" disabled={saving}>
            Send feedback
          </button>
        </form>
      )}
    </div>
  );
}
