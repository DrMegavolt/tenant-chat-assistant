import js from "@eslint/js";
import globals from "globals";

export default [
  {
    ignores: ["coverage/", "node_modules/", "architecture/likec4/"]
  },
  {
    files: ["app.js", "admin.js", "frontend/tests/**/*.js", "vitest.config.js"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      globals: {
        ...globals.browser,
        ...globals.node
      }
    },
    rules: {
      ...js.configs.recommended.rules,
      "no-unused-vars": ["error", { caughtErrors: "none" }]
    }
  }
];
