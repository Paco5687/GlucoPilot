import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import path from 'path'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    // Local dev: proxy API + auth routes to the FastAPI backend.
    proxy: {
      '/api': 'http://localhost:8000',
      '/dexcom': 'http://localhost:8000',
      '/login': 'http://localhost:8000',
      '/setup': 'http://localhost:8000',
      '/logout': 'http://localhost:8000',
    },
  },
})
