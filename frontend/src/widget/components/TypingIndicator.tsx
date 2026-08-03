/**
 * The waiting state, drawn only.
 *
 * `ChatWidget` announces the same state as text in its status region, so these
 * dots are hidden from assistive technology rather than announced three times.
 */
export function TypingIndicator() {
  return (
    <div className="typing" aria-hidden="true">
      <span />
      <span />
      <span />
    </div>
  );
}
