import { CheckIcon } from "src/widget/icons";
import type { CommittedAction } from "src/widget/types";

export interface ActionNoteProps {
  actions: CommittedAction[];
}

/** The user-facing line for a committed action the conversation actually caused. */
const ACTION_COPY: Record<string, string> = {
  book_appointment: "Appointment booked",
  create_lead: "Follow-up request received",
  handoff_to_human: "A team member has been asked to help"
};

function actionLabel(action: CommittedAction): string {
  const base = ACTION_COPY[action.action] ?? "Action completed";
  return action.replayed ? `${base} — already confirmed earlier` : base;
}

/**
 * What a turn actually committed, told the way a customer would understand it.
 *
 * This is the user-appropriate replacement for the prototype's raw tool-call
 * trace: the visitor never needs a tool name or its serialized arguments, only
 * the effect — a booking, a follow-up, a handoff — with its reference. An
 * action the widget does not recognize still degrades to a plain statement,
 * never to its internal name.
 */
export function ActionNote({ actions }: ActionNoteProps) {
  return (
    <ul className="action-note" aria-label="Actions completed">
      {actions.map((action, index) => (
        <li key={`${action.action}-${action.reference}-${index}`}>
          <CheckIcon />
          <span>
            {actionLabel(action)}
            {action.reference && (
              <span className="action-reference">Reference {action.reference}</span>
            )}
          </span>
        </li>
      ))}
    </ul>
  );
}
