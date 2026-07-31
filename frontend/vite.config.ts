import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The backend serves both the JSON API and the HLS stream on :8000. Proxying
// them keeps the dev server same-origin, which matters for the spectrum meter:
// an AnalyserNode reads silence from a cross-origin media element.
const backend = process.env.VITE_BACKEND ?? 'http://localhost:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': { target: backend, changeOrigin: true },
      '/stream': { target: backend, changeOrigin: true },
    },
  },
})
