/** Inline icons. Sized by the stylesheet; every one is decorative. */

import type { JSX } from "react";

const base = {
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round",
  strokeLinejoin: "round",
  "aria-hidden": true,
  focusable: false
} as const satisfies JSX.IntrinsicElements["svg"];

export function ChatIcon() {
  return (
    <svg {...base}>
      <path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4z" />
    </svg>
  );
}

export function CloseIcon() {
  return (
    <svg {...base}>
      <path d="M18 6 6 18M6 6l12 12" />
    </svg>
  );
}

export function SendIcon() {
  return (
    <svg {...base}>
      <path d="m22 2-7 20-4-9-9-4Z" />
      <path d="M22 2 11 13" />
    </svg>
  );
}

export function ArrowDownIcon() {
  return (
    <svg {...base}>
      <path d="M12 5v14M19 12l-7 7-7-7" />
    </svg>
  );
}

export function CheckIcon() {
  return (
    <svg {...base}>
      <path d="M20 6 9 17l-5-5" />
    </svg>
  );
}
