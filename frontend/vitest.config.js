import { defineConfig } from "vitest/config";

// Coverage and JUnit files land under the repository root so CI collects Python
// and frontend results from the same two directories.
export default defineConfig({
  test: {
    environment: "jsdom",
    include: ["tests/**/*.test.js"],
    reporters: ["default", "junit"],
    outputFile: {
      junit: "../artifacts/test-results/frontend.xml"
    },
    coverage: {
      provider: "v8",
      include: ["public/app.js"],
      reporter: ["text", "html", "cobertura", "json-summary"],
      reportsDirectory: "../coverage/frontend"
    }
  }
});
