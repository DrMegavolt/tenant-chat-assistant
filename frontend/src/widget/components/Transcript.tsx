import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";

import { ArrowDownIcon } from "src/widget/icons";

const PINNED_THRESHOLD_PX = 32;

export interface TranscriptProps {
  isBusy: boolean;
  /** Anything that grows the transcript; a change re-evaluates the scroll. */
  revision: number;
  children: ReactNode;
}

/**
 * The scrolling conversation log.
 *
 * It follows new messages only while the visitor is already at the bottom.
 * Yanking somebody back down mid-sentence because staff replied is the way
 * chat transcripts usually lose people; scrolled-up visitors are offered a
 * jump instead.
 *
 * Every scroll here is instant. A smooth one emits scroll events all the way
 * down, and the handler below reads the first of them — far from the bottom —
 * as the visitor having scrolled away, which turns auto-follow off exactly when
 * a message arrives.
 */
export function Transcript({ isBusy, revision, children }: TranscriptProps) {
  const logRef = useRef<HTMLDivElement>(null);
  const [isPinned, setPinned] = useState(true);

  const scrollToLatest = () => {
    const log = logRef.current;
    if (log) log.scrollTop = log.scrollHeight;
  };

  useLayoutEffect(() => {
    if (isPinned) scrollToLatest();
    // `isPinned` is deliberately absent: re-pinning should not scroll on its
    // own, only new content should.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [revision]);

  useEffect(() => {
    const log = logRef.current;
    if (!log) return;
    const onScroll = () => {
      const distance = log.scrollHeight - log.scrollTop - log.clientHeight;
      setPinned(distance <= PINNED_THRESHOLD_PX);
    };
    log.addEventListener("scroll", onScroll, { passive: true });
    return () => log.removeEventListener("scroll", onScroll);
  }, []);

  return (
    <div className="transcript-region">
      <div
        ref={logRef}
        className="transcript"
        id="messages"
        role="log"
        aria-live="polite"
        aria-relevant="additions"
        aria-label="Conversation"
        aria-busy={isBusy}
        tabIndex={0}
      >
        {children}
      </div>
      {!isPinned && (
        <button
          type="button"
          className="jump-to-latest"
          onClick={() => {
            setPinned(true);
            scrollToLatest();
          }}
        >
          <ArrowDownIcon />
          Latest messages
        </button>
      )}
    </div>
  );
}
