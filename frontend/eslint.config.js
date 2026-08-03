import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["node_modules/", "dist/", "coverage/"] },
  {
    files: ["src/**/*.{ts,tsx}", "tests/**/*.{ts,tsx}", "*.config.{js,ts}"],
    extends: [js.configs.recommended, tseslint.configs.recommendedTypeChecked],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "module",
      globals: { ...globals.browser, ...globals.node },
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname
      }
    },
    rules: {
      "no-unused-vars": "off",
      "@typescript-eslint/no-unused-vars": [
        "error",
        { caughtErrors: "none", argsIgnorePattern: "^_" }
      ],
      // A `catch` that ignores the error is deliberate in the storage and
      // polling fallbacks; anything else must be a real type-safe access.
      "@typescript-eslint/consistent-type-imports": "error",
      // Reaching out of a directory is always spelled `src/...`, so an import
      // reads the same wherever the file that holds it is moved to.
      "no-restricted-imports": [
        "error",
        { patterns: [{ group: ["../*"], message: "Use an absolute `src/...` import instead." }] }
      ]
    }
  },
  {
    files: ["src/**/*.tsx", "src/**/use*.ts"],
    extends: [reactHooks.configs.flat.recommended]
  },
  {
    files: ["*.config.{js,ts}"],
    extends: [tseslint.configs.disableTypeChecked]
  }
);
