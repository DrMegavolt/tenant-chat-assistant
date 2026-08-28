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
 *
 * A failed rating stays on screen and stays enabled, announced through an
 * alert: a control that disabled itself on a swallowed error would promise a
 * retry it could never deliver.
 */
export function FeedbackControl({ rating, onRate }: FeedbackControlProps) {
  const controlIds = useId();
  const labelId = `${controlIds}-label`;
  const reasonId = `${controlIds}-reason`;
  const [picking, setPicking] = useState<"up" | "down" | null>(null);
  const [reason, setReason] = useState("");
  const [saving, setSaving] = useState(false);
  const [failed, setFailed] = useState(false);

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
    if (choice === "down" && picking === null) {
      setPicking("down");
      return;
    }
    setSaving(true);
    setFailed(false);
    try {
      await onRate(choice, choice === "down" ? reason.trim() || undefined : undefined);
    } catch {
      setFailed(true);
    } finally {
      setSaving(false);
    }
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
      {failed && (
        <p className="feedback-failure" role="alert">
          That rating could not be recorded. Please try again.
        </p>
      )}
      {picking === "down" && (
        <form
          className="feedback-reason"
          onSubmit={(event) => {
            event.preventDefault();
            void submit("down");
          }}
        >
          <label htmlFor={reasonId}>
            <span className="visually-hidden">What went wrong? (optional)</span>
            <textarea
              id={reasonId}
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
