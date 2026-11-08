import { defineConfig } from "vite";

// The mock reads the shared reference data in ../data.
export default defineConfig({
  server: { fs: { allow: [".."] } },
});
