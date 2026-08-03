export interface QuickActionsProps {
  actions: string[];
  disabled: boolean;
  onPick: (action: string) => void;
}

export function QuickActions({ actions, disabled, onPick }: QuickActionsProps) {
  if (!actions.length) return null;
  return (
    <div className="quick-actions" id="quickActions" role="group" aria-label="Suggested questions">
      {actions.map((action) => (
        <button key={action} type="button" disabled={disabled} onClick={() => onPick(action)}>
          {action}
        </button>
      ))}
    </div>
  );
}
