import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Tauri expects a fixed port.
// `process` is a Node global and @types/node is not a dependency, so read it
// through a typed accessor instead of relying on the ambient Node types.
const host = (
  globalThis as { process?: { env?: Record<string, string | undefined> } }
).process?.env?.TAURI_DEV_HOST;

export default defineConfig(async () => ({
  plugins: [react()],

  // Vite options tailored for Tauri development
  clearScreen: false,
  server: {
    port: 5173,
    strictPort: true,
    host: host || false,
    hmr: host
      ? {
          protocol: "ws",
          host,
          port: 1421,
        }
      : undefined,
    watch: {
      ignored: ["**/src-tauri/**"],
    },
  },
}));
