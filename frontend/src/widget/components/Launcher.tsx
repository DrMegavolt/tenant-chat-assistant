import type { RefObject } from "react";

import { ChatIcon } from "src/widget/icons";

export interface LauncherProps {
  buttonRef: RefObject<HTMLButtonElement | null>;
  isOpen: boolean;
  unreadCount: number;
  onOpen: () => void;
}

export function Launcher({ buttonRef, isOpen, unreadCount, onOpen }: LauncherProps) {
  const unreadLabel = unreadCount === 1 ? "1 new reply" : `${String(unreadCount)} new replies`;
  return (
    <button
      ref={buttonRef}
      type="button"
      className="launcher"
      id="openChat"
      hidden={isOpen}
      aria-expanded={isOpen}
      aria-controls="chatWindow"
      onClick={onOpen}
    >
      <ChatIcon />
      <span className="visually-hidden">
        {unreadCount ? `Open chat, ${unreadLabel}` : "Open chat"}
      </span>
      {unreadCount > 0 && (
        <span className="launcher-badge" aria-hidden="true">
          {unreadCount}
        </span>
      )}
    </button>
  );
}
