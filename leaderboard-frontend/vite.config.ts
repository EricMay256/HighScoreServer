import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // Forward all /api/* requests to the FastAPI dev server.
      // This means the React dev server and FastAPI run in parallel;
      // the browser only ever talks to one origin (localhost:5173),
      // so there are no CORS issues during development.
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    // Emit a manifest so FastAPI can resolve hashed asset filenames
    // if you ever want to serve the built assets from Python directly.
    manifest: true,
  },
})
