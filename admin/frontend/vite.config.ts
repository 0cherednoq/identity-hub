import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import path from "node:path";
import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";

export default defineConfig(({ mode }) => {
  const backendOrigin =
    loadEnv(mode, ".", "").VITE_BACKEND_ORIGIN ?? "http://127.0.0.1:8765";
  return {
    plugins: [react(), tailwindcss()],
    resolve: {
      alias: {
        "@": path.resolve(import.meta.dirname, "./src"),
      },
    },
    build: {
      outDir: "../src/identity_hub_admin/static",
      emptyOutDir: true,
      sourcemap: true,
    },
    server: {
      proxy: {
        "/api": backendOrigin,
        "/health": backendOrigin,
      },
    },
    test: {
      environment: "jsdom",
      setupFiles: ["./src/test/setup.ts"],
      include: ["src/**/*.test.{ts,tsx}"],
    },
  };
});
