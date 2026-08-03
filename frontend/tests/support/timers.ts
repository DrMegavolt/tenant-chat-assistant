import { act } from "@testing-library/react";
import { vi } from "vitest";

/**
 * Advance fake timers and let everything they started finish.
 *
 * A poll is a timer that starts a fetch that resolves through several
 * microtasks before React commits, so one `advanceTimersByTime` is never
 * enough. Wrapping in `act` also keeps React from warning about the updates
 * those promises cause.
 */
export async function tick(ms = 0): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
    await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(0);
  });
}
