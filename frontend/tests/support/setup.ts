import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

// Node gates its built-in localStorage behind `--localstorage-file`, so jsdom
// serves no `window.localStorage` under vitest at all. The demo page's tenant
// memory (N-10) needs one. An in-memory stand-in is faithful for everything
// the suite can observe: values persist for the file's run and vanish with
// the process, like a fresh browser profile.
if (typeof window.localStorage === "undefined") {
  const entries = new Map<string, string>();
  const storage: Storage = {
    get length(): number {
      return entries.size;
    },
    clear: (): void => entries.clear(),
    getItem: (key: string): string | null => (entries.has(key) ? entries.get(key)! : null),
    key: (index: number): string | null => [...entries.keys()][index] ?? null,
    removeItem: (key: string): void => {
      entries.delete(key);
    },
    setItem: (key: string, value: string): void => {
      entries.set(key, String(value));
    }
  };
  // `localStorage` is an accessor on the window, so define rather than assign.
  Object.defineProperty(window, "localStorage", { value: storage, configurable: true });
}

// `globals: false` keeps the vitest API explicit in every file, so React
// Testing Library's automatic cleanup has to be wired up once here instead.
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  // Both storages: a test that exercises the demo page's tenant memory (or a
  // credential) must not leak its choice into the next test in the file.
  window.sessionStorage.clear();
  window.localStorage.clear();
  document.body.innerHTML = "";
});
