import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { AdminPage } from "src/admin/AdminPage";
import { ErrorBoundary } from "src/shared/ErrorBoundary";
import "src/admin/admin.css";

const container = document.querySelector("#root");
if (container) {
  createRoot(container).render(
    <StrictMode>
      <ErrorBoundary>
        <AdminPage />
      </ErrorBoundary>
    </StrictMode>
  );
}
