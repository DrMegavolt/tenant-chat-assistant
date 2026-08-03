import { afterEach, beforeEach, describe, expect, test } from "vitest";

import { resolveApiBaseUrl } from "src/widget/api";

function mountElement(apiBaseUrl?: string): HTMLElement {
  const element = document.createElement("div");
  if (apiBaseUrl !== undefined) element.dataset.apiBaseUrl = apiBaseUrl;
  return element;
}

beforeEach(() => {
  document.head.innerHTML = "";
  document.body.innerHTML = "";
});

afterEach(() => {
  delete window.CHAT_API_BASE_URL;
});

describe("resolving the backend origin for an embed", () => {
  test("a global set by the embedding page wins over every other source", () => {
    window.CHAT_API_BASE_URL = "https://global.example.test";
    document.head.innerHTML = `<script data-api-base-url="https://tag.example.test"></script>`;

    expect(resolveApiBaseUrl(mountElement("https://mount.example.test"))).toBe(
      "https://global.example.test"
    );
  });

  test("a script tag configures the origin even though modules have no currentScript", () => {
    document.head.innerHTML = `<script type="module" data-api-base-url="https://tag.example.test"></script>`;

    expect(document.currentScript).toBeNull();
    expect(resolveApiBaseUrl(mountElement())).toBe("https://tag.example.test");
  });

  test("the mount element is the last configured source", () => {
    expect(resolveApiBaseUrl(mountElement("https://mount.example.test/"))).toBe(
      "https://mount.example.test"
    );
  });

  test("trailing slashes and surrounding whitespace are trimmed", () => {
    window.CHAT_API_BASE_URL = "  https://chat.example.test///  ";

    expect(resolveApiBaseUrl(null)).toBe("https://chat.example.test");
  });

  test("an unconfigured embed uses same-origin relative paths", () => {
    expect(resolveApiBaseUrl(mountElement(""))).toBe("");
  });
});
