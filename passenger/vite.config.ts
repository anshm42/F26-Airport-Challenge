import { resolve } from "node:path";
import { defineConfig } from "vite";

// Two pages: the setup page (index) and the board itself.
export default defineConfig({
  build: {
    rollupOptions: {
      input: {
        index: resolve(import.meta.dirname, "index.html"),
        board: resolve(import.meta.dirname, "board.html"),
      },
    },
  },
});
