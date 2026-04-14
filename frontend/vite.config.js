import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://localhost:5000',
      '/vnc': { target: 'ws://localhost:5000', ws: true },
      '/host-ws': { target: 'ws://localhost:5000', ws: true },
      '/terminal': 'http://localhost:5000',
      '/host-terminal': 'http://localhost:5000',
    }
  },
  build: {
    outDir: 'dist'
  }
})
