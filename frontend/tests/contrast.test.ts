import { describe, expect, test } from "vitest";

import {
  WIDGET_PALETTE,
  paletteDeclarations,
  type ColorScheme,
  type WidgetColors
} from "src/widget/palette";
import { WIDGET_STYLESHEET } from "src/widget/styles";

/**
 * jsdom cannot compute painted colours, so axe's contrast rule is disabled in
 * `accessibility.test.tsx`. These pairs are the ones the widget stylesheet
 * actually renders. Each is checked in both schemes against the WCAG 2.2 AA
 * threshold for how it is used, so darkening a token for the light scheme
 * cannot silently break the dark one.
 */
const TEXT_MINIMUM = 4.5;
const NON_TEXT_MINIMUM = 3;

const PAIRS: [string, keyof WidgetColors, keyof WidgetColors, number][] = [
  ["message text", "ink", "surface", TEXT_MINIMUM],
  ["transcript background text", "ink", "surfaceMuted", TEXT_MINIMUM],
  ["hovered control text", "ink", "surfaceRaised", TEXT_MINIMUM],
  ["staff message", "ink", "staffSurface", TEXT_MINIMUM],
  ["proactive message", "ink", "proactiveSurface", TEXT_MINIMUM],
  ["field labels", "inkMuted", "surface", TEXT_MINIMUM],
  ["privacy bar", "inkMuted", "surfaceMuted", TEXT_MINIMUM],
  ["visitor message", "accentInk", "accent", TEXT_MINIMUM],
  ["header title", "headerInk", "headerBg", TEXT_MINIMUM],
  ["header tagline", "headerInkMuted", "headerBg", TEXT_MINIMUM],
  ["tool call trace", "noticeInk", "noticeSurface", TEXT_MINIMUM],
  ["form error", "dangerInk", "dangerSurface", TEXT_MINIMUM],
  ["link buttons", "accent", "surface", TEXT_MINIMUM],
  ["link buttons over the transcript", "accent", "surfaceMuted", TEXT_MINIMUM],
  ["input and button borders", "line", "surface", NON_TEXT_MINIMUM],
  ["consent field border", "line", "surfaceMuted", NON_TEXT_MINIMUM],
  ["staff bubble edge", "staffLine", "staffSurface", NON_TEXT_MINIMUM],
  ["focus indicator", "focusRing", "surface", NON_TEXT_MINIMUM],
  ["focus indicator over the transcript", "focusRing", "surfaceMuted", NON_TEXT_MINIMUM],
  ["focus indicator on a hovered control", "focusRing", "surfaceRaised", NON_TEXT_MINIMUM]
];

const SCHEMES: ColorScheme[] = ["light", "dark"];

function relativeLuminance(hex: string): number {
  const value = Number.parseInt(hex.slice(1), 16);
  const channels = [(value >> 16) & 255, (value >> 8) & 255, value & 255]
    .map((channel) => channel / 255)
    .map((channel) => (channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4));
  return 0.2126 * channels[0]! + 0.7152 * channels[1]! + 0.0722 * channels[2]!;
}

function contrastRatio(foreground: string, background: string): number {
  const [lighter, darker] = [relativeLuminance(foreground), relativeLuminance(background)].sort(
    (a, b) => b - a
  );
  return (lighter! + 0.05) / (darker! + 0.05);
}

describe("widget colour contrast", () => {
  const cases = SCHEMES.flatMap((scheme) =>
    PAIRS.map(
      ([usage, foreground, background, minimum]) =>
        [scheme, usage, foreground, background, minimum] as const
    )
  );

  test.each(cases)(
    "%s: %s meets WCAG 2.2 AA",
    (scheme, _usage, foreground, background, minimum) => {
      const palette = WIDGET_PALETTE[scheme];
      expect(contrastRatio(palette[foreground], palette[background])).toBeGreaterThanOrEqual(
        minimum
      );
    }
  );

  test("the reference implementation agrees with a known contrast ratio", () => {
    expect(contrastRatio("#ffffff", "#000000")).toBeCloseTo(21, 5);
    expect(contrastRatio("#777777", "#ffffff")).toBeCloseTo(4.48, 2);
  });
});

describe("the palette reaches the stylesheet", () => {
  test("every colour role is published as a scheme-aware custom property", () => {
    const declarations = paletteDeclarations();
    for (const role of Object.keys(WIDGET_PALETTE.light) as (keyof WidgetColors)[]) {
      expect(declarations).toContain(
        `light-dark(${WIDGET_PALETTE.light[role]}, ${WIDGET_PALETTE.dark[role]})`
      );
    }
  });

  test("no rule paints a raw opaque colour the contrast suite cannot see", () => {
    // An opaque colour written straight into a rule is a colour these pairs say
    // nothing about. Translucent shadow and hover values are exempt: they sit
    // over a checked token rather than replacing one.
    const stylesheetRules = WIDGET_STYLESHEET.slice(WIDGET_STYLESHEET.indexOf("}") + 1);
    const literals = stylesheetRules.match(/#[0-9a-f]{3,8}\b|\brgb\(|\bhsl\(/gi) ?? [];

    expect(literals).toEqual([]);
  });
});
