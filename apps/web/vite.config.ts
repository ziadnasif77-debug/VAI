/**
 * Vite configuration (SPEC §9, §50).
 *
 * The dev server proxies `/api` to the local backend so the browser makes
 * same-origin requests: no CORS in development, and the production build is a
 * folder of static files the API can serve itself. Local-first means there is
 * never a second host to configure.
 */

import react from '@vitejs/plugin-react';
import {defineConfig} from 'vite';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Loopback only. §50: nothing here should be reachable from the network.
    host: '127.0.0.1',
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8765',
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
});
