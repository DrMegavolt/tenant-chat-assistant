import { useEffect, useRef, useState } from "react";

export interface PrivacyDisclosureProps {
  /** Erase this tenant's conversation from the browser and start over. */
  onForget: () => void;
}

/**
 * What the widget keeps, said before it is asked for, plus the control that
 * deletes it. Collapsed by default so it is available without being in the way.
 */
export function PrivacyDisclosure({ onForget }: PrivacyDisclosureProps) {
  const [isOpen, setOpen] = useState(false);
  const forgetRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (isOpen) forgetRef.current?.focus();
  }, [isOpen]);

  return (
    <>
      <p className="privacy-bar" id="privacyNote">
        <span>Messages are stored so the team can answer and follow up.</span>
        <button
          type="button"
          className="link-button"
          id="privacyToggle"
          aria-expanded={isOpen}
          aria-controls="privacyPanel"
          onClick={() => setOpen((open) => !open)}
        >
          Privacy and your data
        </button>
      </p>

      <div
        className="privacy-panel"
        id="privacyPanel"
        hidden={!isOpen}
        aria-labelledby="privacyTitle"
      >
        <h2 id="privacyTitle">How this chat uses your data</h2>
        <ul>
          <li>Your messages are stored so staff can read and answer them.</li>
          <li>Anything you type into chat is transmitted and stored.</li>
          <li>Your browser keeps a conversation credential so replies from staff reach you.</li>
          <li>Closing the tab does not delete your conversation; the server keeps it.</li>
        </ul>
        <button ref={forgetRef} type="button" id="clearVisitorData" onClick={onForget}>
          Delete this conversation from my browser
        </button>
      </div>
    </>
  );
}
