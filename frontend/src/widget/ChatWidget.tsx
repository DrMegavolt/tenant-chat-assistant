import { useEffect, useMemo, useRef, type KeyboardEvent } from "react";

import type { ChatApi } from "src/widget/api";
import { BookingConfirmation } from "src/widget/components/BookingConfirmation";
import { Composer } from "src/widget/components/Composer";
import { Launcher } from "src/widget/components/Launcher";
import { MessageBubble } from "src/widget/components/MessageBubble";
import { PrivacyDisclosure } from "src/widget/components/PrivacyDisclosure";
import { QuickActions } from "src/widget/components/QuickActions";
import { ToolTrace } from "src/widget/components/ToolTrace";
import { Transcript } from "src/widget/components/Transcript";
import { TypingIndicator } from "src/widget/components/TypingIndicator";
import { CloseIcon } from "src/widget/icons";
import type { TenantConfig } from "src/widget/types";
import { useConversation } from "src/widget/useConversation";
import { VisitorData, consentStatement } from "src/widget/visitorData";

export interface ChatWidgetProps {
  api: ChatApi;
  tenantId: string;
  config: TenantConfig;
  isOpen: boolean;
  onOpen: () => void;
  onClose: () => void;
}

/** Up to two initials, so the header has a mark even without a logo asset. */
function initials(name: string): string {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((word) => word[0]?.toUpperCase() ?? "")
    .join("");
}

/**
 * One tenant's conversation, launcher and panel together.
 *
 * The component is mounted under a `key` of the tenant id: switching tenants
 * replaces it outright, which is what makes a switched-away conversation
 * unreachable rather than merely hidden.
 */
export function ChatWidget({ api, tenantId, config, isOpen, onOpen, onClose }: ChatWidgetProps) {
  const visitor = useMemo(() => new VisitorData(tenantId), [tenantId]);
  const conversation = useConversation({ api, tenantId, config, visitor, isOpen });

  const inputRef = useRef<HTMLInputElement>(null);
  const launcherRef = useRef<HTMLButtonElement>(null);
  const wasOpen = useRef(isOpen);

  // Focus follows the visitor's own action only. Moving focus on first paint
  // would steal the caret from whatever the host page had focused.
  useEffect(() => {
    if (wasOpen.current === isOpen) return;
    wasOpen.current = isOpen;
    if (isOpen) inputRef.current?.focus();
    else launcherRef.current?.focus();
  }, [isOpen]);

  const { markRead } = conversation;
  useEffect(() => {
    if (isOpen) markRead();
  }, [isOpen, markRead]);

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Escape" || !isOpen) return;
    // The host page never sees this key: it did not open the panel and must not
    // have to know how to close it.
    event.stopPropagation();
    onClose();
  };

  return (
    <div onKeyDown={handleKeyDown}>
      <section
        className="panel"
        id="chatWindow"
        role="dialog"
        aria-labelledby="chatCompany"
        aria-describedby="chatTagline"
        hidden={!isOpen}
      >
        {/* A `header` element here would be a second banner landmark on the
            embedding page; inside a dialog the element buys nothing. */}
        <div className="panel-header">
          <span className="brand-mark" aria-hidden="true">
            {initials(config.name)}
          </span>
          <span className="brand-text">
            <strong id="chatCompany">{config.assistantName}</strong>
            <span id="chatTagline">{config.tagline}</span>
          </span>
          <button type="button" className="icon-button" id="closeChat" onClick={onClose}>
            <CloseIcon />
            <span className="visually-hidden">Close chat</span>
          </button>
        </div>

        <Transcript
          isBusy={conversation.isSending}
          revision={conversation.entries.length + (conversation.isSending ? 1 : 0)}
        >
          {conversation.entries.map((entry) => {
            if (entry.kind === "message") {
              return (
                <MessageBubble
                  key={entry.id}
                  role={entry.role}
                  source={entry.source}
                  text={entry.text}
                />
              );
            }
            if (entry.kind === "tool") {
              return <ToolTrace key={entry.id} event={entry.event} />;
            }
            return (
              <BookingConfirmation
                key={entry.id}
                pending={entry.pending}
                consentStatement={consentStatement(config.name)}
                onDecide={(decision) => conversation.decide(decision)}
              />
            );
          })}
          {conversation.isSending && <TypingIndicator />}
        </Transcript>

        <div className="panel-footer">
          <p className="assistant-status" id="assistantStatus" role="status">
            {conversation.status}
          </p>
          <QuickActions
            actions={config.quickActions}
            disabled={conversation.isSending}
            onPick={(action) => void conversation.send(action)}
          />
          <Composer
            inputRef={inputRef}
            isSending={conversation.isSending}
            onSend={(text) => void conversation.send(text)}
          />
          <PrivacyDisclosure onForget={conversation.forget} />
        </div>
      </section>

      <Launcher
        buttonRef={launcherRef}
        isOpen={isOpen}
        unreadCount={conversation.unreadStaffCount}
        onOpen={onOpen}
      />
    </div>
  );
}
