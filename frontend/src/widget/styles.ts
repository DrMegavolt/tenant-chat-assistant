import { paletteDeclarations } from "src/widget/palette";
import widgetCss from "src/widget/widget.css?inline";

/**
 * The complete stylesheet injected into the widget's shadow root.
 *
 * The palette is prepended rather than written into `widget.css` so the colour
 * tokens have exactly one definition, in TypeScript, where the contrast suite
 * can read them. `all: initial` in the stylesheet's own `:host` block does not
 * clear these: the `all` shorthand never applies to custom properties.
 */
export const WIDGET_STYLESHEET = `:host {\n${paletteDeclarations()}\n}\n\n${widgetCss}`;
