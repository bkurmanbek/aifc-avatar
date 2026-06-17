function readNumber(name: string, fallback: number, min = 0, max = Number.POSITIVE_INFINITY): number {
  const raw = import.meta.env[name] as string | undefined
  const value = raw == null || raw.trim() === '' ? fallback : Number(raw)
  if (!Number.isFinite(value)) return fallback
  return Math.min(max, Math.max(min, value))
}

export const FPS = 25
export const MAX_DOM_MSGS = readNumber('VITE_MAX_DOM_MSGS', 60, 10, 500)
export const IDLE_TIMEOUT_MS = readNumber('VITE_IDLE_TIMEOUT_MS', 30 * 60_000, 60_000, 24 * 60 * 60_000)
export const AUTO_ENDPOINT_MS = readNumber('VITE_AUTO_ENDPOINT_MS', 2000, 500, 6000)
export const MIN_RECORD_MS = readNumber('VITE_MIN_RECORD_MS', 500, 100, 5000)
export const VAD_SILENCE_LEVEL = readNumber('VITE_VAD_SILENCE_LEVEL', 0.10, 0.01, 0.95)
export const CHUNK_GAP_HOLD_MS = readNumber('VITE_CHUNK_GAP_HOLD_MS', 20, 0, 5000)
export const VAD_INTERVAL_MS = readNumber('VITE_VAD_INTERVAL_MS', 60, 20, 250)
export const PARTIAL_SEND_INTERVAL = readNumber('VITE_PARTIAL_SEND_INTERVAL', 5, 1, 30)
export const VAD_CHUNK_SEND_INTERVAL = readNumber('VITE_VAD_CHUNK_SEND_INTERVAL', 1, 1, 30)
export const CANVAS_W = readNumber('VITE_CANVAS_W', 540, 160, 2160)
export const CANVAS_H = readNumber('VITE_CANVAS_H', 960, 160, 3840)
export const RECONNECT_BASE_MS = readNumber('VITE_RECONNECT_BASE_MS', 1500, 250, 30000)
export const RECONNECT_MAX_MS = readNumber('VITE_RECONNECT_MAX_MS', 30000, 1000, 120000)
// Steady retry interval while the backend is "busy" (single-session gate). Not the fast
// base (avoids hammering the box) and not the long backoff (keeps admission prompt once a
// slot frees). Jitter is applied per attempt to desync multiple waiting clients.
export const BUSY_RECONNECT_MS = readNumber('VITE_BUSY_RECONNECT_MS', 3000, 1000, 30000)
// Application-level heartbeat: send a ping every WS_HEARTBEAT_MS and force a reconnect
// if no traffic (pong or any message) arrives within WS_HEARTBEAT_TIMEOUT_MS. Catches
// half-open sockets far faster than the TCP timeout.
export const WS_HEARTBEAT_MS = readNumber('VITE_WS_HEARTBEAT_MS', 5000, 1000, 60000)
export const WS_HEARTBEAT_TIMEOUT_MS = readNumber('VITE_WS_HEARTBEAT_TIMEOUT_MS', 12000, 3000, 120000)
