import { useEffect, useId, useRef, type KeyboardEvent } from "react";

const FOCUSABLE_SELECTOR =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export interface ConfirmDialogProps {
  title: string;
  body: string;
  confirmLabel: string;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * A destructive-action confirmation, as the console renders modals.
 *
 * Same contract as the widget's source viewer: a labelled dialog, focus moved
 * into it on open, Tab trapped inside, Escape cancelling, and focus returned
 * to the control that opened it on close — so a keyboard operator cancelling a
 * delete is not dropped into the top of the page.
 */
export function ConfirmDialog({
  title,
  body,
  confirmLabel,
  onConfirm,
  onCancel
}: ConfirmDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const titleId = useId();

  useEffect(() => {
    // The opener is whoever held focus before the dialog took it, so it must
    // be captured first: reading activeElement after focus() yields the dialog
    // itself, and restoring focus to that detached node drops the operator at
    // <body>. The opener stays mounted behind the dialog; handing focus back
    // to it on close returns them to exactly the row they were working on.
    const opener = dialogRef.current?.ownerDocument.activeElement;
    dialogRef.current?.focus();
    return () => {
      if (opener instanceof HTMLElement) opener.focus();
    };
  }, []);

  const trapFocus = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      onCancel();
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
      className="dialog-backdrop"
      onClick={(event) => {
        if (event.target === event.currentTarget) onCancel();
      }}
    >
      <div
        ref={dialogRef}
        className="confirm-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        onKeyDown={trapFocus}
      >
        <h3 id={titleId}>{title}</h3>
        <p>{body}</p>
        <div className="confirm-dialog-actions">
          <button type="button" className="primary-button" onClick={onConfirm}>
            {confirmLabel}
          </button>
          <button type="button" className="ghost-button" onClick={onCancel}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
