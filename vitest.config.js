import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "jsdom",
    include: ["frontend/tests/**/*.test.js"],
    reporters: ["default", "junit"],
    outputFile: {
      junit: "artifacts/test-results/frontend.xml"
    },
    coverage: {
      provider: "v8",
      include: ["app.js"],
      reporter: ["text", "html", "cobertura", "json-summary"],
      reportsDirectory: "coverage/frontend"
    }
  }
});
