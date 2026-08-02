/**
 * Embed entry point.
 *
 * A customer site includes this one module and a mount element; everything the
 * widget needs comes from `data-` attributes on that element.
 *
 *   <div id="tenant-chat" data-company-id="clearview"></div>
 *   <script type="module" src="https://chat.example.com/embed.js"></script>
 */

import { mountWidget } from "./widget/embed.js";

mountWidget(document.querySelector("#tenant-chat"));
