import { useEffect, useRef, useCallback } from 'react'
import type { WsInbound } from '../types'
import { RECONNECT_BASE_MS, RECONNECT_MAX_MS } from '../constants'

export interface WsHandlers {
  onMessage: (d: WsInbound) => void
  onConnected: () => void
}

export function useWebSocket(handlers: WsHandlers) {
  const wsRef = useRef<WebSocket | null>(null)
  const reconnectDelayRef = useRef(RECONNECT_BASE_MS)
  const handlersRef = useRef(handlers)
  const configuredWsUrl = (import.meta.env.VITE_WS_URL as string | undefined)?.trim()

  useEffect(() => {
    handlersRef.current = handlers
  }, [handlers])

  const sendWs = useCallback((payload: unknown) => {
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(payload))
  }, [])

  useEffect(() => {
    let cancelled = false

    function connect() {
      if (cancelled) return
      const defaultProtocol = window.location.protocol === 'https:' ? 'wss' : 'ws'
      const wsUrl = configuredWsUrl || `${defaultProtocol}://${window.location.host}/ws`
      const ws = new WebSocket(wsUrl)
      wsRef.current = ws

      ws.onopen = () => {
        if (cancelled) { ws.close(); return }
        handlersRef.current.onConnected()
        reconnectDelayRef.current = RECONNECT_BASE_MS
      }

      ws.onclose = () => {
        wsRef.current = null
        if (cancelled) return
        window.setTimeout(connect, reconnectDelayRef.current)
        reconnectDelayRef.current = Math.min(reconnectDelayRef.current * 1.6, RECONNECT_MAX_MS)
      }

      ws.onerror = () => {
        // onclose will fire after onerror
      }

      ws.onmessage = (e) => {
        try {
          const d = JSON.parse(e.data) as WsInbound
          handlersRef.current.onMessage(d)
        } catch {
          // ignore malformed messages
        }
      }
    }

    connect()

    return () => {
      cancelled = true
      wsRef.current?.close()
    }
  }, [configuredWsUrl])

  return { sendWs, wsRef }
}
