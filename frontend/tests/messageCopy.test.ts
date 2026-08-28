import { describe, expect, test } from "vitest";

import { displayCopy } from "src/widget/components/MessageBubble";

describe("displayCopy", () => {
  test("a space before a period is removed", () => {
    expect(displayCopy("word .")).toBe("word.");
  });

  test("spaces before sentence punctuation are removed", () => {
    expect(displayCopy("Hello , world !")).toBe("Hello, world!");
    expect(displayCopy("ready ; set : go ?")).toBe("ready; set: go?");
  });

  test("doubled spaces collapse to one", () => {
    expect(displayCopy("too  many  spaces")).toBe("too many spaces");
  });

  test("punctuation followed by a closing quote is tightened", () => {
    expect(displayCopy('He said "hi ."')).toBe('He said "hi."');
  });

  test("copy that is already tidy is returned unchanged", () => {
    expect(displayCopy("Fine. Done! Really? Yes; now: go.")).toBe(
      "Fine. Done! Really? Yes; now: go."
    );
  });

  test("punctuation inside a URL is never touched", () => {
    // A query "?" or a path "." never has whitespace before it, so the rule
    // cannot reach inside a URL.
    expect(displayCopy("Visit https://example.com/docs?a=1&b=2 today")).toBe(
      "Visit https://example.com/docs?a=1&b=2 today"
    );
  });

  test("a stray space before a period after a URL trims only that space", () => {
    // Only the whitespace before the punctuation mark itself is removed; the
    // URL is left exactly as sent, query mark included.
    expect(displayCopy("See https://example.com/pricing?a=1 .")).toBe(
      "See https://example.com/pricing?a=1."
    );
  });

  test("an empty string stays empty", () => {
    expect(displayCopy("")).toBe("");
  });
});
