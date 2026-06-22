import { defineConfig } from "vite";

// Static SPA. Vercel serves the built `dist/` and runs `api/*.ts` as serverless functions
// alongside it (so /api/token is reachable from the page). `vercel dev` wires both locally.
export default defineConfig({
  server: { port: 5273 },
  build: { outDir: "dist" },
});
