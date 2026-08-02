import js from "@eslint/js";
import globals from "globals";

export default [
  {
    ignores: ["node_modules/"]
  },
  {
    files: ["public/**/*.js", "tests/**/*.js", "*.config.js"],
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
