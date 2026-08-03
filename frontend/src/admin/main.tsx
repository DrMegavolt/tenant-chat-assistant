import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { AdminPage } from "src/admin/AdminPage";
import "src/admin/admin.css";

const container = document.querySelector("#root");
if (container) {
  createRoot(container).render(
    <StrictMode>
      <AdminPage />
    </StrictMode>
  );
}
