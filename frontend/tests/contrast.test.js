import { describe, expect, test } from "vitest";

import { WIDGET_PALETTE } from "../public/widget/styles.js";

/**
 * jsdom cannot compute painted colors, so axe's contrast rule is disabled in
 * `accessibility.test.js`. These pairs are the ones the widget stylesheet
 * actually renders; each is checked against the WCAG 2.2 AA threshold for how
 * it is used, so darkening a token in one place cannot silently break another.
 */
const TEXT_MINIMUM = 4.5;
const NON_TEXT_MINIMUM = 3;

const PAIRS = [
  ["message text", "ink", "surface", TEXT_MINIMUM],
  ["transcript background text", "ink", "surfaceMuted", TEXT_MINIMUM],
  ["staff message", "ink", "staffSurface", TEXT_MINIMUM],
  ["proactive message", "ink", "proactiveSurface", TEXT_MINIMUM],
  ["field labels", "inkMuted", "surface", TEXT_MINIMUM],
  ["privacy bar", "inkMuted", "surfaceMuted", TEXT_MINIMUM],
  ["visitor message", "accentInk", "accent", TEXT_MINIMUM],
  ["header title", "headerInk", "headerBg", TEXT_MINIMUM],
  ["header tagline", "headerInkMuted", "headerBg", TEXT_MINIMUM],
  ["tool call trace", "noticeInk", "noticeSurface", TEXT_MINIMUM],
  ["form error", "dangerInk", "dangerSurface", TEXT_MINIMUM],
  ["input and button borders", "line", "surface", NON_TEXT_MINIMUM],
  ["consent field border", "line", "surfaceMuted", NON_TEXT_MINIMUM],
  ["focus indicator", "focusRing", "surface", NON_TEXT_MINIMUM],
  ["focus indicator over the transcript", "focusRing", "surfaceMuted", NON_TEXT_MINIMUM],
  ["link buttons", "accent", "surface", TEXT_MINIMUM]
];

function relativeLuminance(hex) {
  const value = Number.parseInt(hex.slice(1), 16);
  const channels = [(value >> 16) & 255, (value >> 8) & 255, value & 255]
    .map((channel) => channel / 255)
    .map((channel) => (channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4));
  return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
}

function contrastRatio(foreground, background) {
  const [lighter, darker] = [relativeLuminance(foreground), relativeLuminance(background)].sort(
    (a, b) => b - a
  );
  return (lighter + 0.05) / (darker + 0.05);
}

describe("widget color contrast", () => {
  test.each(PAIRS)("%s meets WCAG 2.2 AA", (_usage, foreground, background, minimum) => {
    const ratio = contrastRatio(WIDGET_PALETTE[foreground], WIDGET_PALETTE[background]);
    expect(ratio).toBeGreaterThanOrEqual(minimum);
  });

  test("the reference implementation agrees with a known contrast ratio", () => {
    expect(contrastRatio("#ffffff", "#000000")).toBeCloseTo(21, 5);
    expect(contrastRatio("#777777", "#ffffff")).toBeCloseTo(4.48, 2);
  });
});
