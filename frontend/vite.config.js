import { defineConfig } from "vite";

/**
 * Development server for `frontend/public/`.
 *
 * The browser loads the same unbundled ES modules the nginx image serves, so
 * what hot-reloads here is what ships: this config adds a file watcher and an
 * API proxy, and no build step.
 *
 * Production paths are reproduced exactly:
 * - `/` serves the public frontend (root: public/).
 * - `/admin/` serves the admin frontend from the same root (alias).
 * - `/api` public routes proxy to the public backend listener.
 * - `/api/admin/` and `/api/leads` proxy to the admin backend listener.
 * - Widget CORS and admin auth are handled by the gateway in production;
 *   in development the prototype serves both on one port.
 */
const backendOrigin = process.env.CHAT_DEV_BACKEND_ORIGIN ?? "http://127.0.0.1:8000";
const adminOrigin = process.env.CHAT_DEV_ADMIN_ORIGIN ?? backendOrigin;

function adminPathPlugin() {
  const rewrites = new Map([
    ["/admin", "/admin.html"],
    ["/admin/", "/admin.html"],
    ["/admin/admin.js", "/admin.js"],
    ["/admin/styles.css", "/styles.css"]
  ]);

  return {
    name: "tenantchat-admin-paths",
    configureServer(server) {
      server.middlewares.use((request, _response, next) => {
        if (!request.url) return next();
        const parsed = new URL(request.url, "http://vite.local");
        const replacement = rewrites.get(parsed.pathname);
        if (replacement) request.url = `${replacement}${parsed.search}`;
        return next();
      });
    }
  };
}

export default defineConfig({
  root: "public",
  appType: "mpa",
  plugins: [adminPathPlugin()],
  server: {
    // Explicit loopback: the default `localhost` resolves to ::1 on machines
    // where the 127.0.0.1 URLs printed elsewhere in this repository then fail.
    host: "127.0.0.1",
    port: Number(process.env.CHAT_DEV_PORT ?? 5173),
    strictPort: true,
    proxy: {
      // Admin API routes proxy to the admin listener (in production, the
      // gateway auth-gates these before forwarding).
      "/api/admin/": {
        target: adminOrigin,
        changeOrigin: false
      },
      "/api/leads": {
        target: adminOrigin,
        changeOrigin: false
      },
      // OAuth callback (auth proxy in production; no-op in dev).
      "/oauth2/callback": {
        target: adminOrigin,
        changeOrigin: false
      },
      // Public visitor API routes proxy to the public listener.
      "/api": {
        target: backendOrigin,
        changeOrigin: false
      }
    }
  }
});
