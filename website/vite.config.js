import { defineConfig } from 'vite'

// Static marketing site for emtext. No framework — plain ES modules + Vite so
// each section can live in its own file and be built independently.
export default defineConfig({
  server: { port: 5173, open: false },
  build: { target: 'es2022', cssMinify: true },
})
