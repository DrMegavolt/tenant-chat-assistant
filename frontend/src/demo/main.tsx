import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { DemoPage } from "src/demo/DemoPage";
import { ErrorBoundary } from "src/shared/ErrorBoundary";
import "src/demo/demo.css";

const container = document.querySelector("#root");
const host = document.querySelector<HTMLElement>("#tenant-chat");

if (container && host) {
  createRoot(container).render(
    <StrictMode>
      <ErrorBoundary>
        <DemoPage host={host} />
      </ErrorBoundary>
    </StrictMode>
  );
}
