/**
 * What a visitor sees when the backend cannot be reached at all.
 *
 * A visitor whose backend is down must be told so in a live region rather than
 * left with a chat box that silently swallows every message.
 */
export function FatalError({ message }: { message: string }) {
  return (
    <div className="widget-error" role="alert" tabIndex={-1}>
      <h2>Chat is unavailable</h2>
      <p>{message}</p>
    </div>
  );
}
