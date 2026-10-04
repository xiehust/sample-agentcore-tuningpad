import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  // TUNINGPAD_API lets a dev instance point at a backend on another port
  const env = loadEnv(mode, ".", "TUNINGPAD_");
  const proxy = { "/api": env.TUNINGPAD_API ?? "http://localhost:8100" };
  return {
    plugins: [react()],
    server: { proxy },
    preview: { proxy },
  };
});
