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

  test("an evidence tag that failed validation never reaches the visitor", () => {
    // The live failure (N-06): the model wrote a malformed marker — a space
    // after the colon, spaces in the label — which matches neither the
    // evidence catalog nor the server's narrow strip rule, so the raw tag
    // shipped in the reply text. It is display-only markup and must not
    // render.
    expect(displayCopy("Our Saturday hours are 9:00 AM - 2:00 PM [evidence: business facts]")).toBe(
      "Our Saturday hours are 9:00 AM - 2:00 PM"
    );
  });

  test("a mid-sentence evidence tag is removed without leaving a double space", () => {
    expect(displayCopy("We service furnaces [evidence: service list] on weekdays.")).toBe(
      "We service furnaces on weekdays."
    );
  });

  test("a well-formed evidence marker is dropped too; chips carry the citation", () => {
    // The server normally strips these before publishing; removing any that
    // survive costs nothing because the citation chips render from the turn's
    // validated citation list, never from the text.
    expect(displayCopy("Plans cover tune-ups [evidence:src-hvac-guide].")).toBe(
      "Plans cover tune-ups."
    );
  });

  test("text that only mentions evidence, or brackets of another kind, is untouched", () => {
    expect(displayCopy("The word evidence on its own stays.")).toBe(
      "The word evidence on its own stays."
    );
    expect(displayCopy("A [source: label] of another kind stays.")).toBe(
      "A [source: label] of another kind stays."
    );
  });
});
