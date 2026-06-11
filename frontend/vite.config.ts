import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const readEnv = (key: string) => env[key] || process.env[key]
  const backendPort = readEnv('WS_BACKEND_PORT') || '8080'
  const backendHttpTarget = readEnv('VITE_BACKEND_HTTP_URL') || `http://localhost:${backendPort}`
  const backendWsTarget = readEnv('VITE_BACKEND_WS_URL') || `ws://localhost:${backendPort}`

  console.info(`[vite] proxy /ws -> ${backendWsTarget}`)
  console.info(`[vite] proxy /chat,/health -> ${backendHttpTarget}`)

  return {
    plugins: [react()],
    server: {
      proxy: {
        '/ws': {
          target: backendWsTarget,
          ws: true
        },
        '/chat': {
          target: backendHttpTarget,
          changeOrigin: true
        },
        '/health': {
          target: backendHttpTarget,
          changeOrigin: true
        },
        '/intro-cache': {
          target: backendHttpTarget,
          changeOrigin: true
        }
      }
    }
  }
})
