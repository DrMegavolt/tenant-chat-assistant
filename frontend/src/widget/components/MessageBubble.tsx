import type { MessageRole, MessageSource } from "src/widget/types";

export interface MessageBubbleProps {
  role: MessageRole;
  source: MessageSource;
  text: string;
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
      <span>{text}</span>
    </div>
  );
}
