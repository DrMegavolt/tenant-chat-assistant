import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Coverage and JUnit files land under the repository root so CI collects Python
// and frontend results from the same two directories.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      src: fileURLToPath(new URL("./src", import.meta.url)),
      tests: fileURLToPath(new URL("./tests", import.meta.url))
    }
  },
  test: {
    environment: "jsdom",
    globals: false,
    include: ["tests/**/*.test.{ts,tsx}"],
    setupFiles: ["tests/support/setup.ts"],
    // The widget ships its stylesheet as a string inside the shadow root, so the
    // suite has to see the real CSS rather than Vitest's default empty stub.
    css: true,
    reporters: ["default", "junit"],
    outputFile: {
      junit: "../artifacts/test-results/frontend.xml"
    },
    coverage: {
      provider: "v8",
      include: ["src/**/*.{ts,tsx}"],
      exclude: ["src/**/main.ts", "src/**/main.tsx"],
      reporter: ["text", "html", "cobertura", "json-summary"],
      reportsDirectory: "../coverage/frontend"
    }
  }
});
