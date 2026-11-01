import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

// Tauri loads the dev server at a fixed port and the built files from dist/ (src-tauri/tauri.conf.json).
//
// In development each component injects its own CSS (emitCss: false). With separate CSS modules, a webview that
// kept the compiled components from an earlier dev server asks the new one for their CSS before it has compiled
// them; the plugin then can't answer and Vite serves the raw .svelte file as CSS ("failed to load virtual css
// module"), which leaves styles unscoped and drops each component's first rule. Builds still emit CSS files.
export default defineConfig(({ command }) => ({
  plugins: [svelte({ emitCss: command === "build" })],
  clearScreen: false,
  server: { port: 5173, strictPort: true, host: "127.0.0.1" },
  build: { target: "es2022", sourcemap: false },
  test: { environment: "jsdom", include: ["tests/**/*.test.js"] },
}));
