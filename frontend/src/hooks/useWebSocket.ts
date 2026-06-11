import { useCallback, useEffect, useRef } from 'react'
import type { WsInbound } from '../types'
import { RECONNECT_BASE_MS, RECONNECT_MAX_MS } from '../constants'

let pageIntroToken: string | null = null

function getIntroToken(namespace: string) {
  if (pageIntroToken) return pageIntroToken
  pageIntroToken = `${namespace}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
  return pageIntroToken
}

function withIntroToken(rawUrl: string, rawToken: string) {
  const token = encodeURIComponent(rawToken)
  const joiner = rawUrl.includes('?') ? '&' : '?'
  return `${rawUrl}${joiner}intro_token=${token}`
}

export interface WsHandlers {
  onMessage: (d: WsInbound) => void
  onConnected: () => void
  onDisconnected?: (event: { code?: number; reason?: string; wasClean?: boolean }) => void
  onError?: (event: { source: string; message: string; detail?: unknown }) => void
}

export function useWebSocket(handlers: WsHandlers) {
  const wsRef = useRef<WebSocket | null>(null)
  const reconnectDelayRef = useRef(RECONNECT_BASE_MS)
  const reconnectTimerRef = useRef<number | null>(null)
  const handlersRef = useRef(handlers)
  const fallbackAttemptedRef = useRef(false)
  const connectRef = useRef<(targetUrl?: string) => void>(() => {})
  const configuredWsUrl = (import.meta.env.VITE_WS_URL as string | undefined)?.trim()
  const introTokenNamespace = [
    window.location.origin,
    configuredWsUrl || '',
    (import.meta.env.VITE_AVATAR_LABEL as string | undefined)?.trim() || '',
  ].join('|')
  const introToken = getIntroToken(introTokenNamespace)
  const defaultProtocol = window.location.protocol === 'https:' ? 'wss' : 'ws'
  const fallbackWsUrl = `${defaultProtocol}://${window.location.host}/ws`
  const primaryWsUrl = withIntroToken(configuredWsUrl || fallbackWsUrl, introToken)
  const fallbackWsUrlWithToken = withIntroToken(fallbackWsUrl, introToken)
  const shouldTryFallback = Boolean(configuredWsUrl && configuredWsUrl !== fallbackWsUrl)
  const activeTargetRef = useRef(primaryWsUrl)

  useEffect(() => {
    handlersRef.current = handlers
  }, [handlers])

  const clearReconnectTimer = useCallback(() => {
    if (reconnectTimerRef.current != null) {
      window.clearTimeout(reconnectTimerRef.current)
      reconnectTimerRef.current = null
    }
  }, [])

  const sendWs = useCallback((payload: unknown) => {
    const ws = wsRef.current
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      handlersRef.current.onError?.({
        source: 'websocket.send',
        message: 'websocket send skipped - socket is not open',
        detail: { readyState: ws?.readyState ?? null },
      })
      return false
    }
    try {
      ws.send(JSON.stringify(payload))
      return true
    } catch (error) {
      handlersRef.current.onError?.({
        source: 'websocket.send',
        message: 'websocket send failed',
        detail: error,
      })
      return false
    }
  }, [])

  const scheduleReconnect = useCallback(
    (disconnect: { code?: number; reason?: string; wasClean?: boolean }, wsUrl: string, sourceWs?: WebSocket) => {
      if (sourceWs && wsRef.current !== sourceWs) {
        console.debug('[websocket] ignoring close from stale socket', { wsUrl, disconnect })
        return
      }

      wsRef.current = null
      handlersRef.current.onDisconnected?.(disconnect)

      if (shouldTryFallback && !fallbackAttemptedRef.current && wsUrl !== fallbackWsUrlWithToken) {
        fallbackAttemptedRef.current = true
        activeTargetRef.current = fallbackWsUrlWithToken
        handlersRef.current.onError?.({
          source: 'websocket.fallback',
          message: 'primary websocket failed - trying local fallback',
          detail: { from: wsUrl, to: fallbackWsUrlWithToken },
        })
        reconnectTimerRef.current = window.setTimeout(() => {
          connectRef.current(fallbackWsUrlWithToken)
        }, 250)
        return
      }

      activeTargetRef.current = primaryWsUrl
      reconnectDelayRef.current = Math.min(reconnectDelayRef.current * 1.6, RECONNECT_MAX_MS)
      handlersRef.current.onError?.({
        source: 'websocket.reconnect',
        message: `websocket reconnecting in ${Math.round(reconnectDelayRef.current)}ms`,
        detail: { target: primaryWsUrl, delayMs: reconnectDelayRef.current },
      })
      reconnectTimerRef.current = window.setTimeout(() => {
        connectRef.current(primaryWsUrl)
      }, reconnectDelayRef.current)
    },
    [fallbackWsUrlWithToken, primaryWsUrl, shouldTryFallback],
  )

  const connect = useCallback((targetUrl?: string) => {
    if (targetUrl) {
      activeTargetRef.current = targetUrl
    }

    clearReconnectTimer()
    const wsUrl = activeTargetRef.current || fallbackWsUrlWithToken
    let ws: WebSocket
    try {
      ws = new WebSocket(wsUrl)
    } catch (error) {
      handlersRef.current.onError?.({
        source: 'websocket.invalid_url',
        message: `invalid websocket url: ${wsUrl}`,
        detail: error,
      })
      scheduleReconnect({ code: 0, reason: `invalid websocket url: ${wsUrl}`, wasClean: false }, wsUrl)
      return
    }
    wsRef.current = ws

    ws.onopen = () => {
      if (wsRef.current !== ws) {
        console.debug('[websocket] ignoring open from stale socket', { wsUrl })
        ws.close()
        return
      }
      if (shouldTryFallback && wsUrl === fallbackWsUrlWithToken) {
        fallbackAttemptedRef.current = false
        activeTargetRef.current = fallbackWsUrlWithToken
      }
      if (!shouldTryFallback) {
        activeTargetRef.current = wsUrl
      }
      handlersRef.current.onConnected()
      reconnectDelayRef.current = RECONNECT_BASE_MS
      fallbackAttemptedRef.current = false
    }

    ws.onclose = (event) => {
      scheduleReconnect(
        {
          code: event.code,
          reason: event.reason,
          wasClean: event.wasClean,
        },
        wsUrl,
        ws,
      )
    }
  
    ws.onerror = () => {
      if (wsRef.current !== ws) {
        console.debug('[websocket] ignoring error from stale socket', { wsUrl })
        return
      }
      handlersRef.current.onError?.({
        source: 'websocket.error',
        message: 'websocket error before close',
        detail: { target: wsUrl, readyState: ws.readyState },
      })
    }

    ws.onmessage = (event) => {
      if (wsRef.current !== ws) {
        console.debug('[websocket] ignoring message from stale socket', { wsUrl })
        return
      }
      try {
        const d = JSON.parse(event.data) as WsInbound
        handlersRef.current.onMessage(d)
      } catch (error) {
        handlersRef.current.onError?.({
          source: 'websocket.message',
          message: 'malformed websocket message ignored',
          detail: {
            error,
            sample: typeof event.data === 'string' ? event.data.slice(0, 200) : '[non-string data]',
          },
        })
      }
    }
  }, [clearReconnectTimer, fallbackWsUrlWithToken, scheduleReconnect, shouldTryFallback])

  useEffect(() => {
    connectRef.current = connect
  }, [connect])

  useEffect(() => {
    connect(primaryWsUrl)

    return () => {
      clearReconnectTimer()
      const ws = wsRef.current
      wsRef.current = null
      ws?.close()
      fallbackAttemptedRef.current = false
      activeTargetRef.current = primaryWsUrl
    }
  }, [connect, clearReconnectTimer, primaryWsUrl])

  return { sendWs, wsRef }
}
