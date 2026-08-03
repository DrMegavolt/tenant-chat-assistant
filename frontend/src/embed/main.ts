/**
 * Embed entry point — the file a customer site includes.
 *
 * Its name is fixed by the build (`embed.js`, never hashed) because that URL is
 * pasted into other people's HTML and cannot change between releases.
 *
 *   <div id="tenant-chat" data-company-id="clearview"></div>
 *   <script type="module" src="https://chat.example.com/embed.js"></script>
 */

import { mountWidget } from "src/widget/mount";

const host = document.querySelector<HTMLElement>("#tenant-chat");
if (host) mountWidget(host);
