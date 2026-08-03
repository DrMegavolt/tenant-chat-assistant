import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

// `globals: false` keeps the vitest API explicit in every file, so React
// Testing Library's automatic cleanup has to be wired up once here instead.
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  window.sessionStorage.clear();
  document.body.innerHTML = "";
});
