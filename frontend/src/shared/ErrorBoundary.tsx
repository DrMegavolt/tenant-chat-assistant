import { Component, type ErrorInfo, type ReactNode } from "react";

interface ErrorBoundaryProps {
  /**
   * What to render instead of the crashed subtree. Omit for a minimal,
   * self-styled panel that reads correctly in a document, a shadow root, or
   * an unstyled page.
   */
  fallback?: ReactNode;
  children: ReactNode;
}

interface ErrorBoundaryState {
  crashed: boolean;
}

/**
 * One render crash must cost one view, not the whole page.
 *
 * Without a boundary, any bad field or unexpected shape in a response
 * white-screens the incident console and the visitor's widget alike; with one,
 * the rest of the page keeps working and the failure is stated where it
 * happened. The error itself goes to the browser console, where the operator
 * or integrator can read it.
 */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  override state: ErrorBoundaryState = { crashed: false };

  static getDerivedStateFromError(): ErrorBoundaryState {
    return { crashed: true };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error(
      "The view crashed and was replaced by an error notice.",
      error,
      info.componentStack
    );
  }

  override render(): ReactNode {
    if (this.state.crashed) {
      return (
        this.props.fallback ?? (
          <div
            role="alert"
            style={{
              all: "initial",
              display: "block",
              fontFamily: "system-ui, sans-serif",
              padding: "16px",
              border: "1px solid #b3261e",
              borderRadius: "8px",
              color: "#1b1b1b",
              background: "#fff",
              maxWidth: "32rem"
            }}
          >
            <p style={{ margin: "0 0 8px", fontWeight: 600 }}>
              Something went wrong displaying this view.
            </p>
            <p style={{ margin: "0 0 12px" }}>
              The failure is logged in the browser console. Reloading usually recovers it.
            </p>
            <button type="button" onClick={() => window.location.reload()}>
              Reload
            </button>
          </div>
        )
      );
    }
    return this.props.children;
  }
}
