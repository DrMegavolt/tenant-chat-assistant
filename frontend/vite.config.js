import { defineConfig } from "vite";

/**
 * Development server for `frontend/public/`.
 *
 * The browser loads the same unbundled ES modules the nginx image serves, so
 * what hot-reloads here is what ships: this config adds a file watcher and an
 * API proxy, and no build step.
 *
 * `/api` is proxied rather than pointed at with `CHAT_API_BASE_URL` so the app
 * runs same-origin in development exactly as it does behind nginx. Configuring
 * a cross-origin base instead would exercise a CORS path that production does
 * not have.
 */
const backendOrigin = process.env.CHAT_DEV_BACKEND_ORIGIN ?? "http://127.0.0.1:8000";

export default defineConfig({
  root: "public",
  appType: "mpa",
  server: {
    // Explicit loopback: the default `localhost` resolves to ::1 on machines
    // where the 127.0.0.1 URLs printed elsewhere in this repository then fail.
    host: "127.0.0.1",
    port: Number(process.env.CHAT_DEV_PORT ?? 5173),
    strictPort: true,
    proxy: {
      "/api": {
        target: backendOrigin,
        // The backend binds 127.0.0.1 and checks no Host header; forwarding the
        // dev server's own Host keeps its logs readable.
        changeOrigin: false
      }
    }
  }
});
