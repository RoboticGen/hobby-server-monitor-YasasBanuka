import { defineConfig } from 'astro/config';

// https://astro.build/config
export default defineConfig({
  // Output: hybrid mode — most pages are static, but dynamic routes (e.g. /containers/[name])
  // are server-rendered. In production, Falcon serves the static files and handles
  // the server-rendered routes via the Node.js adapter... actually for simplicity,
  // we use client-side JS to load data for dynamic pages in static output mode.
  // Dynamic routes use a catch-all pattern instead.
  output: 'static',

  // During development, Astro runs on port 4321 and proxies API calls to Falcon on 8000.
  // This avoids CORS issues in development.
  vite: {
    server: {
      allowedHosts: true,
      proxy: {
        '/api': {
          target: 'http://localhost:8000',
          changeOrigin: true,
        },
        '/healthz': {
          target: 'http://localhost:8000',
          changeOrigin: true,
        },
      },
    },
    preview: {
      allowedHosts: ['13.223.242.47.nip.io', 'localhost'],
    },
  },

  // Base path (change if served under a subpath)
  base: '/',
});
