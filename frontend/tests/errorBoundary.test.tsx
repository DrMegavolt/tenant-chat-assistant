import { render, screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { ErrorBoundary } from "src/shared/ErrorBoundary";

function Bomb({ message }: { message: string }): never {
  throw new Error(message);
}

describe("the error boundary", () => {
  test("a render crash renders an honest notice instead of a blank page", () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <ErrorBoundary>
        <Bomb message="cannot read field of undefined" />
      </ErrorBoundary>
    );

    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("Something went wrong");
    expect(alert.textContent).toContain("Reload");
    // The error itself is on the console where an operator can read it, not
    // swallowed.
    expect(consoleError).toHaveBeenCalled();
    consoleError.mockRestore();
  });

  test("a custom fallback replaces the crashed subtree", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <ErrorBoundary fallback={<p role="alert">The chat could not be displayed.</p>}>
        <Bomb message="boom" />
      </ErrorBoundary>
    );
    expect(screen.getByRole("alert").textContent).toBe("The chat could not be displayed.");
  });

  test("a healthy subtree renders untouched", () => {
    render(
      <ErrorBoundary>
        <p>Everything is fine.</p>
      </ErrorBoundary>
    );
    expect(screen.getByText("Everything is fine.")).toBeTruthy();
  });
});
