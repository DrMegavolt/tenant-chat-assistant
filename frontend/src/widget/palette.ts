/**
 * The widget's colour tokens, in both schemes it can render.
 *
 * The widget lives inside a shadow root on somebody else's page, so it cannot
 * inherit a palette and cannot assume a background: every colour it paints is
 * declared here. Declaring them once in TypeScript and generating the custom
 * properties from that object keeps a token and its usage from drifting apart,
 * and lets `tests/contrast.test.ts` prove every rendered pair meets WCAG 2.2 AA
 * in *both* schemes rather than only in the one a reviewer happened to open.
 */

export type ColorScheme = "light" | "dark";

/** Every colour role the widget stylesheet is allowed to reference. */
export interface WidgetColors {
  /** Panels, bubbles, and form fields. */
  surface: string;
  /** The transcript behind the bubbles, and inset areas inside panels. */
  surfaceMuted: string;
  /** Hover and pressed states on otherwise transparent controls. */
  surfaceRaised: string;
  ink: string;
  inkMuted: string;
  /** Borders that are a control's only visible boundary (WCAG 2.2 §1.4.11). */
  line: string;
  /** Decorative separators, which carry no information and need no contrast. */
  hairline: string;
  accent: string;
  accentInk: string;
  headerBg: string;
  headerInk: string;
  headerInkMuted: string;
  dangerSurface: string;
  dangerInk: string;
  noticeSurface: string;
  noticeInk: string;
  proactiveSurface: string;
  staffSurface: string;
  staffLine: string;
  focusRing: string;
  shadow: string;
}

export const WIDGET_PALETTE: Record<ColorScheme, WidgetColors> = {
  light: {
    surface: "#ffffff",
    surfaceMuted: "#f2f5f6",
    surfaceRaised: "#e7edee",
    ink: "#101f22",
    inkMuted: "#4c585c",
    line: "#78868a",
    hairline: "#e2e8e9",
    accent: "#0b5f59",
    accentInk: "#ffffff",
    headerBg: "#102328",
    headerInk: "#ffffff",
    headerInkMuted: "#c3cdcf",
    dangerSurface: "#fff3ea",
    dangerInk: "#8a3a05",
    noticeSurface: "#fdf4e6",
    noticeInk: "#6b3c11",
    proactiveSurface: "#ebf3f5",
    staffSurface: "#fdf5e3",
    staffLine: "#a67c19",
    focusRing: "#0b5f59",
    shadow: "rgba(16, 31, 34, 0.18)"
  },
  dark: {
    surface: "#182022",
    surfaceMuted: "#10171a",
    surfaceRaised: "#232d30",
    ink: "#e9f0f1",
    inkMuted: "#a5b3b7",
    line: "#7d8d91",
    hairline: "#2b3639",
    accent: "#5fd6c6",
    accentInk: "#04211e",
    headerBg: "#0b1214",
    headerInk: "#f3f8f9",
    headerInkMuted: "#b6c3c6",
    dangerSurface: "#2d1a10",
    dangerInk: "#f7b183",
    noticeSurface: "#2a2113",
    noticeInk: "#efc384",
    proactiveSurface: "#152a2e",
    staffSurface: "#2a2415",
    staffLine: "#8a7136",
    focusRing: "#7fe3d5",
    shadow: "rgba(0, 0, 0, 0.55)"
  }
};

/** The custom-property name a colour role is published under. */
export function tokenName(role: keyof WidgetColors): string {
  return `--${role.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`)}`;
}

/**
 * The `:host` declarations that publish the palette to the widget stylesheet.
 *
 * Each role becomes one `light-dark()` custom property, so the stylesheet names
 * a role and the browser picks the scheme. Emitted as a string because the
 * stylesheet is injected into the shadow root as text.
 */
export function paletteDeclarations(): string {
  return (Object.keys(WIDGET_PALETTE.light) as (keyof WidgetColors)[])
    .map(
      (role) =>
        `  ${tokenName(role)}: light-dark(${WIDGET_PALETTE.light[role]}, ${WIDGET_PALETTE.dark[role]});`
    )
    .join("\n");
}
