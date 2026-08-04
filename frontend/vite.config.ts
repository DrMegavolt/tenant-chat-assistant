import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig, type BuildEnvironmentOptions, type Plugin } from "vite";

/**
 * Build and development server for the browser application.
 *
 * Three build passes, because the deployment has three audiences:
 *
 *   vite build                -> dist/public  the demo host page
 *   vite build --mode embed   -> dist/public  embed.js, added to the same root
 *   vite build --mode admin   -> dist/admin   the operator console, base /admin/
 *
 * Public and admin are separate builds because the isolation between the two
 * document roots is the security argument: `/srv/public` is on the internet and
 * `/srv/admin` is behind the auth proxy. One build would emit shared hashed
 * chunks that both roots need, and the only way to serve them would be to
 * publish admin code from the public root.
 *
 * The embed is separate for a different reason. It is loaded cross-origin from
 * customer sites, and a module script's imports are fetched under CORS: every
 * shared chunk would need its own allowlisted response. One self-contained file
 * with a stable, unhashed name is both the simplest thing to allowlist and the
 * only URL that can safely be pasted into somebody else's HTML.
 *
 * The dev server reproduces the production paths:
 * - `/` serves the demo host page.
 * - `/admin/` serves the operator console.
 * - `/api` public routes proxy to the API listener (`make api`, port 8080).
 * - `/api/admin/` routes to the same listener; the gateway auth-gates them in
 *   production, and the API fail-closes locally when no gateway identity is
 *   sent, so the console's login screen is the expected dev behavior.
 * - Widget CORS and admin auth belong to the gateway in production; in
 *   development the API serves both audiences on one port.
 */
const backendOrigin = process.env.CHAT_DEV_BACKEND_ORIGIN ?? "http://127.0.0.1:8080";
const adminOrigin = process.env.CHAT_DEV_ADMIN_ORIGIN ?? backendOrigin;

const resolvePath = (relative: string) => fileURLToPath(new URL(relative, import.meta.url));

/** Serve the console at `/admin/` in development, as nginx does in production. */
function adminPathPlugin(): Plugin {
  const rewrites = new Map([
    ["/admin", "/admin.html"],
    ["/admin/", "/admin.html"],
    ["/admin/index.html", "/admin.html"]
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

/**
 * Per-target build options.
 *
 * Source maps are off everywhere: the public root is on the internet, and
 * nothing is gained by publishing a megabyte of readable source from it.
 */
function buildFor(mode: string): BuildEnvironmentOptions {
  const common = { sourcemap: false, assetsDir: "assets" } as const;

  if (mode === "admin") {
    return {
      ...common,
      outDir: "dist/admin",
      emptyOutDir: true,
      rollupOptions: { input: { admin: resolvePath("./admin.html") } }
    };
  }

  if (mode === "embed") {
    return {
      ...common,
      outDir: "dist/public",
      // The public build runs first and owns emptying this directory.
      emptyOutDir: false,
      rollupOptions: {
        input: { embed: resolvePath("./src/embed/main.ts") },
        output: { codeSplitting: false, entryFileNames: "embed.js" }
      }
    };
  }

  return {
    ...common,
    outDir: "dist/public",
    emptyOutDir: true,
    rollupOptions: { input: { index: resolvePath("./index.html") } }
  };
}

export default defineConfig(({ mode }) => ({
  plugins: [react(), adminPathPlugin()],
  base: mode === "admin" ? "/admin/" : "/",
  resolve: { alias: { src: resolvePath("./src") } },
  build: buildFor(mode),
  server: {
    // Explicit loopback: the default `localhost` resolves to ::1 on machines
    // where the 127.0.0.1 URLs printed elsewhere in this repository then fail.
    host: "127.0.0.1",
    port: Number(process.env.CHAT_DEV_PORT ?? 5173),
    strictPort: true,
    proxy: {
      // Admin API routes proxy to the backend (in production, the gateway
      // auth-gates these before forwarding).
      "/api/admin/": { target: adminOrigin, changeOrigin: false },
      // OAuth callback (auth proxy in production; no-op in dev).
      "/oauth2/callback": { target: adminOrigin, changeOrigin: false },
      // Public visitor API routes proxy to the API listener, including the
      // visitor lead-capture POST.  `/api` ordering keeps the admin prefix
      // above this catch-all.
      "/api": { target: backendOrigin, changeOrigin: false }
    }
  }
}));
