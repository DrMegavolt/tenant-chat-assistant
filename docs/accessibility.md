# Widget accessibility

The embeddable widget targets WCAG 2.2 AA (`FEAT-013`). This page records what
is enforced automatically, what must be checked by hand, and the result of the
last manual pass.

## What the build enforces

`make js-test` fails on any of these.

| Check | Where |
| --- | --- |
| axe-core has no violations for the idle widget, the booking form, and the failure state | `frontend/tests/accessibility.test.js` |
| Focus returns to the launcher on close and to the composer on open | `frontend/tests/accessibility.test.js` |
| Escape closes the widget from inside the shadow root | `frontend/tests/accessibility.test.js` |
| The transcript is a polite `log` and reports `aria-busy` while a reply is pending | `frontend/tests/accessibility.test.js` |
| Every message carries a speaker name for a non-sighted listener | `frontend/tests/accessibility.test.js` |
| Every color pair the widget paints meets the AA ratio for its use | `frontend/tests/contrast.test.js` |
| Consent is required and explained before contact details are sent | `frontend/tests/privacy.test.js` |
| Details already given are not requested twice (§3.3.7) | `frontend/tests/privacy.test.js` |
| The widget renders no markup into the host document | `frontend/tests/widget.test.js` |

axe cannot judge `color-contrast`, `target-size`, or `scrollable-region-focusable`
under jsdom, which has no layout engine. Contrast is covered by the ratio test
above; target size and the focusable transcript are covered by the minimum
dimensions and `tabindex` in `frontend/public/widget/styles.js` and
`widget.js`, and by the manual pass below.

## Manual pass

Repeat this list whenever the widget shell, the booking form, or the privacy
panel changes. Record the date and result at the bottom.

### Keyboard only

1. Tab from the host page into the widget; confirm the focus ring is visible on
   every stop and never invisible against its background.
2. Reach the launcher, activate it with Enter and with Space; the composer takes
   focus both times.
3. Press Escape from the composer, from a quick action, and from inside the
   booking form; the widget closes and the launcher takes focus.
4. Send a message with Enter from the composer.
5. Tab into the transcript and scroll it with the arrow keys.
6. Open the privacy panel with the keyboard and reach the delete control.
7. Complete a booking without touching the pointer, including the consent
   checkbox with Space.
8. Submit the booking without consent; focus lands on the checkbox and the error
   is reachable.

### Screen reader

Run with VoiceOver (Safari) and NVDA (Firefox).

1. The launcher announces its name and expanded state.
2. Opening announces the dialog with the assistant's name.
3. A new assistant message is announced without moving focus.
4. "Waiting for the assistant to reply" is announced when a turn is in flight.
5. Each bubble announces its speaker before its text.
6. The consent checkbox announces the full statement, including the company name.
7. A booking error is announced as an alert.
8. The backend-unavailable state is announced as an alert.

### Viewport and motion

1. 320 px wide: no horizontal scrolling, the widget fills the viewport, and all
   controls stay reachable.
2. 400 px tall landscape: the widget goes full screen rather than clipping.
3. 200% browser zoom on a desktop viewport: no content is lost or overlapped.
4. With "reduce motion" enabled at the OS level, the transcript jumps rather
   than smooth-scrolls.
5. Windows high-contrast mode: the window, launcher, and booking card keep a
   visible boundary.

### Host-page isolation

1. Embed on a page whose `body` sets `font-family`, `line-height`, `color`, and
   `* { box-sizing: content-box }`; the widget is unaffected.
2. Embed on a page with an element id of `messages` or `composer`; neither page
   nor widget breaks.
3. Confirm the host page's own styles are unchanged with the widget mounted.

## Last manual pass

Date: 2026-08-02, Chromium at 1280×800, 740×400, and 320×568.

Passed:

- Tab order enters the widget in visual order; the focus ring is visible on the
  quick actions against white and on the close button against the dark header.
- Escape closes the widget and returns focus to the launcher; opening returns
  focus to the composer.
- 320×568 and 740×400: the widget takes the full viewport, the transcript and
  composer both stay usable, and neither the page nor the widget scrolls
  horizontally.
- Every control measures at least 24×24 CSS px (§2.5.8); the smallest is the
  inline privacy link at 26 px tall.
- Host isolation: with the embedding page forcing `font-family`, `font-size`,
  `line-height`, `color`, `letter-spacing`, and `box-sizing` through `!important`
  on `body` and `*`, and declaring its own `#messages`, `#composer`, and
  `#chatInput` elements, nothing reached the widget and neither side broke.

Outstanding:

- Screen-reader list (items 1–8) — no VoiceOver or NVDA session has been run
  against this build.
- Reduced motion and Windows high-contrast mode are implemented in the
  stylesheet but have not been observed under those OS settings.
- 200% browser zoom was not exercised directly; the 320 px viewport covers the
  equivalent layout width but not text-only reflow.
