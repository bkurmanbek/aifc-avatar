
import { useState, useEffect, useRef, useCallback, useReducer } from 'react'
import type { WsInbound, UiMode } from './types'
import { detectUiLanguage, encodeBase64, backendHttpUrl } from './utils'
import { MicVAD } from '@ricky0123/vad-web'
import type { RealTimeVADOptions } from '@ricky0123/vad-web'
import { IDLE_TIMEOUT_MS, VAD_SILENCE_LEVEL, AUTO_ENDPOINT_MS, MIN_RECORD_MS, VAD_INTERVAL_MS } from './constants'
import { activeListeningConfig } from './activeListeningConfig'
import { useWebSocket } from './hooks/useWebSocket'
import { useChunkPlayback } from './hooks/useChunkPlayback'
import { useIdleTimer } from './hooks/useIdleTimer'
import { writeClientLog, type ClientLogLevel } from './services/clientLogger'
import { AvatarStage } from './components/AvatarStage'
import { ChatPanel } from './components/ChatPanel'
import { FloatingChatComposer } from './components/FloatingChatComposer'
import { SidebarCard } from './components/SidebarCard'
import { StatusBar } from './components/StatusBar'
import { ThemeToggle } from './components/ThemeToggle'
import { conversationReducer, initialConversationState } from './state/conversationReducer'
import './styles.css'

type ActiveVadState = 'inactive' | 'initializing' | 'monitoring' | 'recording' | 'processing' | 'paused'

export default function App() {
  const [mode, setMode] = useState<UiMode | string>('idle')
  const [, setStatusText] = useState('connecting')
  const [conversation, dispatchConversation] = useReducer(conversationReducer, initialConversationState)
  const messages = conversation.messages
  const aiMessageId = conversation.aiMessageId
  const [partialText, setPartialText] = useState('')
  const [inputText, setInputText] = useState('')
  const [isBusy, setIsBusy] = useState(false)
  const [introActive, setIntroActive] = useState(false)
  const [isListening, setIsListening] = useState(false)
  const [logText, setLogText] = useState('initialising')
  const [logClass, setLogClass] = useState('')
  const [micEnabled, setMicEnabled] = useState(true)
  const [activeListening, setActiveListening] = useState(false)
  const [darkMode, setDarkMode] = useState(true)
  const [showChat, setShowChat] = useState(true)
  const [showComposer, setShowComposer] = useState(false)
  const [showLeftPanel, setShowLeftPanel] = useState(true)
  const [showRightPanel, setShowRightPanel] = useState(true)
  const [showSettings, setShowSettings] = useState(false)
  const [connectedAt, setConnectedAt] = useState<number | null>(null)
  const [connectedSeconds, setConnectedSeconds] = useState(0)
  const [sttReady, setSttReady] = useState(false)
  const [awaitingIntroTap, setAwaitingIntroTap] = useState(false)
  const [lastLatencyMs, setLastLatencyMs] = useState<number | null>(null)
  // Single-pipeline guard: backend admits one session at a time. When busy, show a
  // "please wait" overlay; the WS reconnect loop keeps retrying until a slot frees.
  const [busyWaiting, setBusyWaiting] = useState(false)

  // ── Refs ──────────────────────────────────────────────────────
  const idleVidRef = useRef<HTMLVideoElement | null>(null)
  const introVidRef = useRef<HTMLVideoElement | null>(null)
  const speakCvsRef = useRef<HTMLCanvasElement | null>(null)
  const vadHolderRef = useRef<HTMLDivElement | null>(null)
  const stageStackRef = useRef<HTMLDivElement | null>(null)
  const sendWsRef = useRef<(payload: unknown) => boolean>(() => false)
  const stopPlaybackRef = useRef<() => void>(() => {})
  const isBusyRef = useRef(isBusy)
  const isListeningRef = useRef(isListening)
  const activeListeningRef = useRef(activeListening)
  const micEnabledRef = useRef(micEnabled)
  const sttReadyRef = useRef(sttReady)
  const pendingActiveListeningRef = useRef(false)
  const pendingPromptRef = useRef<string | null>(null)
  const reconnectPromptRetryRef = useRef<number | null>(null)
  const isSocketOpenRef = useRef(false)
  const currentSessionIdRef = useRef<string | null>(null)
  const activeTurnIdRef = useRef<string | null>(null)
  const showChatRef = useRef(false)
  const lastIntroUrlRef = useRef<string | null>(null)
  // Browsers block autoplay-with-sound until a user gesture. We hold the intro until the
  // first tap/click, then play it with audio (see the gesture effect below).
  const userInteractedRef = useRef(false)
  const pendingIntroUrlRef = useRef<string | null>(null)
  const awaitingIntroTapRef = useRef(false)
  // The intro MP4 lives on the ngrok-tunneled backend, which serves a browser-warning
  // interstitial (text/html) to plain <video> requests — a <video src> can't send the
  // skip header, so it fails with MEDIA_ERR_SRC_NOT_SUPPORTED. We instead fetch the MP4
  // via fetch() WITH the skip header and play it from a blob object URL.
  const introObjUrlRef = useRef<string | null>(null)
  const introBlobPromiseRef = useRef<Promise<string | null> | null>(null)
  // Prebuilt FAQ answer clip (reuses the intro <video> element). faqVideoActiveRef lets the
  // 'done' handler skip the canvas onAllDone->idle while the clip owns the speaking state.
  const faqVideoActiveRef = useRef(false)
  const faqObjUrlRef = useRef<string | null>(null)
  const idleTimerRef = useRef<{ reset: () => void; clear: () => void }>({ reset: () => {}, clear: () => {} })

  useEffect(() => {
    isBusyRef.current = isBusy
    isListeningRef.current = isListening
    activeListeningRef.current = activeListening
    micEnabledRef.current = micEnabled
    sttReadyRef.current = sttReady
    awaitingIntroTapRef.current = awaitingIntroTap
  }, [isBusy, isListening, activeListening, micEnabled, sttReady, awaitingIntroTap])

  // ── Helpers ───────────────────────────────────────────────────
  const log = useCallback((text: string, cls?: string) => {
    setLogText(text)
    setLogClass(cls ?? '')
  }, [])

  const connectionTime = `${String(Math.floor(connectedSeconds / 60)).padStart(2, '0')}:${String(connectedSeconds % 60).padStart(2, '0')}`

  const addUserMsg = useCallback((text: string) => {
    dispatchConversation({ type: 'user_message', text, language: detectUiLanguage(text) })
    setShowChat(true)
  }, [])

  const beginAssistantMsg = useCallback(() => {
    dispatchConversation({ type: 'response_start' })
  }, [])

  const appendAssistantText = useCallback((text: string) => {
    dispatchConversation({ type: 'response_chunk', text })
  }, [])

  const sendTextPayload = useCallback((text: string) => {
    return sendWsRef.current({ type: 'text', text })
  }, [])

  const emitClientLog = useCallback((level: ClientLogLevel, source: string, message: string, detail?: unknown) => {
    writeClientLog({ level, source, message, detail, turn_id: activeTurnIdRef.current })
    sendWsRef.current({
      type: 'client_log',
      level,
      source,
      message,
      detail,
      turn_id: activeTurnIdRef.current,
      session_id: currentSessionIdRef.current,
    })
  }, [])

  const isStaleTurn = useCallback((turnId?: string) => {
    return Boolean(turnId && activeTurnIdRef.current !== turnId)
  }, [])

  // Stop and hide the shared intro/FAQ <video> (used on barge-in, interrupt, error, or new
  // turn). Also tears down any in-flight FAQ clip state + its object URL so an externally
  // triggered stop (the clip never reaches onended) doesn't leak the blob or leave the
  // active flag set.
  const stopIntroVideo = useCallback(() => {
    const v = introVidRef.current
    if (v) {
      v.onended = null
      v.onerror = null
      try { v.pause() } catch { /* ignore */ }
      v.removeAttribute('src')
      try { v.load() } catch { /* ignore */ }
    }
    faqVideoActiveRef.current = false
    if (faqObjUrlRef.current) {
      URL.revokeObjectURL(faqObjUrlRef.current)
      faqObjUrlRef.current = null
    }
    setIntroActive(false)
  }, [])

  // Fetch the intro MP4 as a blob (WITH the ngrok skip header that a <video src> can't
  // send) and expose it as an object URL. Deduped + cached for the page lifetime.
  const loadIntroBlob = useCallback((url: string): Promise<string | null> => {
    if (introObjUrlRef.current) return Promise.resolve(introObjUrlRef.current)
    if (introBlobPromiseRef.current) return introBlobPromiseRef.current
    const p = fetch(backendHttpUrl(url), { headers: { 'ngrok-skip-browser-warning': 'true' } })
      .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.blob() })
      .then((blob) => {
        const obj = URL.createObjectURL(blob)
        introObjUrlRef.current = obj
        return obj
      })
      .catch((err) => {
        // eslint-disable-next-line no-console
        console.warn('[intro] blob fetch failed', err)
        introBlobPromiseRef.current = null
        return null
      })
    introBlobPromiseRef.current = p
    return p
  }, [])

  // Free the intro-MP4 blob on unmount so it isn't pinned in memory beyond the page's
  // life (the browser also revokes on navigation; this covers embedded/non-kiosk use).
  useEffect(() => () => {
    if (introObjUrlRef.current) {
      URL.revokeObjectURL(introObjUrlRef.current)
      introObjUrlRef.current = null
    }
  }, [])

  // Warm the blob ahead of the tap so play() inside the gesture is instant.
  const preloadIntro = useCallback((url: string) => {
    void loadIntroBlob(url)
  }, [loadIntroBlob])

  // Play (or replay) the prebuilt intro MP4. Started from the tap-to-start overlay's
  // onClick, which establishes the sticky user activation that lets it play with sound
  // (the activation persists across the async blob fetch).
  const playIntroVideo = useCallback((url: string) => {
    const v = introVidRef.current
    if (!v) return
    setIsBusy(true)
    isBusyRef.current = true
    setMode('speaking')
    setIntroActive(true)
    let settled = false
    const finish = (reason: string, cls: string = 'ok') => {
      if (settled) return
      settled = true
      v.onended = null
      v.onerror = null
      setIntroActive(false)
      setMode('idle')
      setIsBusy(false)
      isBusyRef.current = false
      log(`intro ${reason}`, cls)
      // eslint-disable-next-line no-console
      console.info('[intro]', reason, { code: v.error?.code, networkState: v.networkState, readyState: v.readyState, src: v.currentSrc })
    }
    const startPlayback = (srcUrl: string) => {
      if (settled) return
      v.onended = () => finish('done')
      v.onerror = () => finish(`error code=${v.error?.code ?? '?'}`, 'err')
      if (v.getAttribute('src') !== srcUrl) { v.src = srcUrl; try { v.load() } catch { /* ignore */ } }
      try { v.currentTime = 0 } catch { /* ignore */ }
      v.muted = false
      const p = v.play()
      if (p) {
        p.then(() => log('intro playing', 'ok')).catch((e: unknown) => {
          // Autoplay-with-sound blocked — show the clip muted rather than nothing.
          const name = e instanceof Error ? e.name : String(e)
          // eslint-disable-next-line no-console
          console.warn('[intro] play() rejected, retrying muted:', name)
          v.muted = true
          v.play().then(() => log('intro playing (muted)', 'ok')).catch((e2: unknown) => {
            finish(`blocked ${e2 instanceof Error ? e2.name : String(e2)}`, 'err')
          })
        })
      }
    }
    if (introObjUrlRef.current) {
      startPlayback(introObjUrlRef.current)
    } else {
      void loadIntroBlob(url).then((obj) => {
        if (obj) startPlayback(obj)
        else finish('fetch failed', 'err')
      })
    }
  }, [log, loadIntroBlob])

  // Play a prebuilt FAQ answer clip in the shared intro <video> element. Mirrors
  // playIntroVideo but fetches a fresh per-URL blob (FAQ URLs vary; the intro reuses one
  // cached blob) and revokes it when the clip ends/stops. By the time a FAQ clip arrives
  // the user has already interacted (asked a question), so sticky activation permits sound.
  const playCachedVideo = useCallback((url: string) => {
    const v = introVidRef.current
    if (!v) return
    faqVideoActiveRef.current = true
    setIsBusy(true)
    isBusyRef.current = true
    setMode('speaking')
    setIntroActive(true)
    let settled = false
    const finish = (reason: string, cls: string = 'ok') => {
      if (settled) return
      settled = true
      v.onended = null
      v.onerror = null
      faqVideoActiveRef.current = false
      if (faqObjUrlRef.current) {
        URL.revokeObjectURL(faqObjUrlRef.current)
        faqObjUrlRef.current = null
      }
      setIntroActive(false)
      setMode('idle')
      setIsBusy(false)
      isBusyRef.current = false
      log(`faq video ${reason}`, cls)
    }
    const startPlayback = (srcUrl: string) => {
      if (settled) return
      v.onended = () => finish('done')
      v.onerror = () => finish(`error code=${v.error?.code ?? '?'}`, 'err')
      v.src = srcUrl
      try { v.load() } catch { /* ignore */ }
      try { v.currentTime = 0 } catch { /* ignore */ }
      v.muted = false
      const p = v.play()
      if (p) {
        p.then(() => log('faq video playing', 'ok')).catch(() => {
          // Autoplay-with-sound blocked — fall back to muted rather than nothing.
          v.muted = true
          v.play().then(() => log('faq video playing (muted)', 'ok')).catch((e2: unknown) => {
            finish(`blocked ${e2 instanceof Error ? e2.name : String(e2)}`, 'err')
          })
        })
      }
    }
    fetch(backendHttpUrl(url), { headers: { 'ngrok-skip-browser-warning': 'true' } })
      .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.blob() })
      .then((blob) => {
        if (settled) return
        const obj = URL.createObjectURL(blob)
        faqObjUrlRef.current = obj
        startPlayback(obj)
      })
      .catch((err) => finish(`fetch failed ${err instanceof Error ? err.message : String(err)}`, 'err'))
  }, [log])


  const sendTextPrompt = useCallback((text: string) => {
    setInputText('')
    const sent = sendTextPayload(text)
    if (!sent) {
      pendingPromptRef.current = text
      isBusyRef.current = false
      setIsBusy(false)
      setMode('idle')
      log('websocket not connected - retrying', 'err')
    } else {
      pendingPromptRef.current = null
      addUserMsg(text)
      setIsBusy(true)
      isBusyRef.current = true
      setMode('thinking')
    }
  }, [addUserMsg, log, sendTextPayload])

  const flushPendingPrompt = useCallback(() => {
    const pendingText = pendingPromptRef.current
    if (!pendingText || isBusyRef.current) return
    if (!isSocketOpenRef.current) return
    pendingPromptRef.current = null
    sendTextPrompt(pendingText)
  }, [sendTextPrompt])

  // ── WebSocket ──────────────────────────────────────────────────
  const ws = useWebSocket({
    onBinaryFrame: (chunk, turnId, jpeg) => {
      playbackRef.current.onFrameBinary(chunk, turnId, jpeg)
    },
    onMessage: (msg: WsInbound) => {
      switch (msg.type) {
        case 'session_state':
          currentSessionIdRef.current = msg.session_id
          setBusyWaiting(false)   // we got a slot
          break
        case 'busy':
          // Another visitor holds the single pipeline slot. Show the wait overlay;
          // the server closes the socket and useWebSocket's reconnect loop retries.
          setBusyWaiting(true)
          break
        case 'evicted':
          // We were dropped (e.g. idle). The reconnect loop will try to rejoin and
          // will land on 'busy' or 'session_state' depending on availability.
          log(msg.text || 'session ended', 'err')
          break
        case 'partial':
          setPartialText(msg.text)
          break
        case 'transcript':
          if (isListeningRef.current && !activeListeningRef.current) micRef.current.stopMic()
          setPartialText('')
          addUserMsg(msg.text)
          setMode('thinking')
          setIsBusy(true)  // Hold busy across the transcript→response_start gap
          break
        case 'transcript_empty':
          if (isListeningRef.current && !activeListeningRef.current) micRef.current.stopMic()
          setPartialText('')
          setIsBusy(false)
          isBusyRef.current = false
          setMode('idle')
          log('could not hear - try again', 'err')
          break
        case 'stt_ready':
          setSttReady(true)
          sttReadyRef.current = true
          log('speech ready', 'ok')
          if (pendingActiveListeningRef.current) {
            pendingActiveListeningRef.current = false
            void micRef.current.ensureActiveListening()
          }
          break
        case 'response_start':
          activeTurnIdRef.current = msg.turn_id ?? null
          setAwaitingIntroTap(false)
          pendingIntroUrlRef.current = null
          stopIntroVideo()
          playbackRef.current.startStream(msg.turn_id)
          beginAssistantMsg()
          showChatRef.current = true
          setShowChat(true)
          setIsBusy(true)
          setMode('thinking')
          break
        case 'response_chunk':
          if (isStaleTurn(msg.turn_id)) return
          appendAssistantText(msg.text)
          // Debounce setShowChat to avoid flooding React with renders during
          // rapid streaming. First message ensures visibility immediately.
          if (!showChatRef.current) {
            showChatRef.current = true
            setShowChat(true)
          }
          break
        case 'answer_payload':
          if (isStaleTurn(msg.turn_id)) return
          {
            const nextAnswer = {
              answer_id: msg.answer_id,
              spoken: msg.spoken,
              chat: msg.chat,
            }
            dispatchConversation({
              type: 'answer_payload',
              answer: nextAnswer,
              formattedText: msg.chat || msg.spoken,
            })
          }
          setShowChat(true)
          break
        case 'policy_state':
          if (!activeTurnIdRef.current && msg.turn_id) activeTurnIdRef.current = msg.turn_id
          if (isStaleTurn(msg.turn_id)) return
          if (msg.answer_language === 'en' || msg.answer_language === 'ru' || msg.answer_language === 'kk' || msg.answer_language === 'zh') {
            dispatchConversation({ type: 'set_answer_language', language: msg.answer_language })
          }
          break
        case 'audio_ready': {
          if (isStaleTurn(msg.turn_id)) return
          const chunk = msg.chunk ?? 0
          if (msg.audio_url) {
            // Intro path: fetch WAV via HTTP as ArrayBuffer — avoids main-thread atob() blocking
            playbackRef.current.onAudioReadyUrl(chunk, msg.audio_url, msg.frame_stride ?? 1, msg.turn_id, Boolean(msg.cached), msg.expected_frames)
          } else if (msg.data) {
            playbackRef.current.onAudioReady(chunk, msg.data, msg.frame_stride ?? 1, msg.turn_id, Boolean(msg.cached), msg.expected_frames)
          }
          break
        }
        case 'frame': {
          if (isStaleTurn(msg.turn_id)) return
          const chunk = msg.chunk ?? 0
          playbackRef.current.onFrame(chunk, msg.data, msg.turn_id)
          break
        }
        case 'frame_cache': {
          if (isStaleTurn(msg.turn_id)) return
          const chunk = msg.chunk ?? 0
          playbackRef.current.onFrameCache(chunk, msg.url, msg.turn_id)
          break
        }
        case 'intro_video': {
          if (isStaleTurn(msg.turn_id)) return
          // Hardware-decoded intro clip — bypasses the canvas frame pipeline entirely.
          lastIntroUrlRef.current = msg.url
          // Autoplay-with-sound is blocked before a user gesture. If the user hasn't
          // interacted yet, buffer the clip and show the full-screen "Tap to start"
          // overlay; its onClick (a real gesture) plays it with audio. If the user has
          // already interacted this session, play immediately.
          if (userInteractedRef.current) {
            playIntroVideo(msg.url)
            // Listen during the intro, not after — same as the tap path (startIntro).
            void micRef.current.ensureActiveListening()
          } else {
            pendingIntroUrlRef.current = msg.url
            preloadIntro(msg.url)
            setAwaitingIntroTap(true)
          }
          break
        }
        case 'faq_video': {
          if (isStaleTurn(msg.turn_id)) return
          // Prebuilt FAQ answer clip — hardware-decoded, bypasses the canvas frame pipeline.
          // The canvas stream started on response_start gets zero frames; stop it and let
          // the clip own the speaking state (the 'done' handler skips onAllDone while
          // faqVideoActiveRef is set). Listen during the clip so barge-in still works.
          playbackRef.current.stopPlayback()
          playCachedVideo(msg.url)
          void micRef.current.ensureActiveListening()
          break
        }
        case 'chunk_done': {
          if (isStaleTurn(msg.turn_id)) return
          const chunk = msg.chunk ?? 0
          playbackRef.current.onChunkDone(chunk, msg.turn_id)
          break
        }
        case 'media_error': {
          if (isStaleTurn(msg.turn_id)) return
          const chunk = msg.chunk ?? 0
          playbackRef.current.onChunkError(chunk, msg.turn_id)
          log(msg.text, 'err')
          break
        }
        case 'done':
          if (isStaleTurn(msg.turn_id)) return
          dispatchConversation({ type: 'done' })
          {
            const total = msg.latency_ms?.total
            const totalMs = typeof total === 'number' ? total : typeof total === 'string' ? Number(total) : NaN
            if (Number.isFinite(totalMs)) setLastLatencyMs(totalMs)
          }
          log(`${msg.chunks ?? 1} chunk(s)`, 'ok')
          emitClientLog('info', 'pipeline.done', 'turn completed', { turnId: msg.turn_id, latencyMs: msg.latency_ms })
          // A FAQ-video turn produces zero canvas chunks; onAllDone(0) would fire
          // onAllChunksDone -> idle immediately, cutting the clip short. The clip's own
          // onended drives idle/busy instead.
          if (!faqVideoActiveRef.current) playbackRef.current.onAllDone(msg.chunks ?? 1)
          break
        case 'status':
          if (isStaleTurn(msg.turn_id)) return
          log(msg.text)
          if (!playbackRef.current.isPlayingRef.current) {
            const s = msg.text.toLowerCase()
            setMode(s.includes('think') || s.includes('generat') ? 'thinking' : 'idle')
          }
          break
        case 'stop_confirmed':
          // Backend detected "Stop" / "Стоп" — halt TTS and return to listening.
          // Do NOT send interrupt back — the backend already cancelled everything.
          // Reset VAD state so onSpeechStart will work for the next turn.
          activeTurnIdRef.current = null
          stopPlaybackRef.current()
          stopIntroVideo()
          isBusyRef.current = false
          setIsBusy(false)
          setMode('idle')
          setPartialText('')
          log('stopped', 'ok')
          break
        case 'interrupted':
          activeTurnIdRef.current = null
          playbackRef.current.stopPlayback()
          stopIntroVideo()
          dispatchConversation({ type: 'interrupted' })
          setIsBusy(false)
          setMode('idle')
          log('')
          break
        case 'error':
          if (isStaleTurn(msg.turn_id)) return
          if (isListeningRef.current && !activeListeningRef.current) micRef.current.stopMic()
          activeTurnIdRef.current = null
          dispatchConversation({ type: 'interrupted' })
          setIsBusy(false)
          playbackRef.current.stopPlayback()
          stopIntroVideo()
          setMode('idle')
          log(msg.text, 'err')
          break
        default:
          break
      }
    },
    onConnected: useCallback(() => {
      setMode('idle')
      setStatusText('ready')
      setSttReady(false)
      sttReadyRef.current = false
      isSocketOpenRef.current = true
      activeTurnIdRef.current = null
      pendingActiveListeningRef.current = false
      flushPendingPrompt()
      setConnectedAt(Date.now())
      setConnectedSeconds(0)
      log('ready')
  }, [log, flushPendingPrompt]),
    onDisconnected: useCallback((disconnectEvent) => {
      isSocketOpenRef.current = false
      currentSessionIdRef.current = null
      activeTurnIdRef.current = null
      stopPlaybackRef.current()
      setMode('idle')
      setStatusText('connecting')
      setConnectedAt(null)
      setConnectedSeconds(0)
      setIsBusy(false)
      isBusyRef.current = false
      const reasonBits = [
        disconnectEvent.code != null ? `code ${disconnectEvent.code}` : null,
        disconnectEvent.reason ? disconnectEvent.reason : null,
        disconnectEvent.wasClean != null ? `clean ${disconnectEvent.wasClean}` : null,
      ].filter(Boolean)
      const suffix = reasonBits.length > 0 ? ` (${reasonBits.join(', ')})` : ''
      log(`websocket disconnected${suffix}`, 'err')
    }, [log]),
    onError: useCallback((event) => {
      emitClientLog(event.source === 'websocket.reconnect' ? 'info' : 'warning', event.source, event.message, event.detail)
      log(event.message, event.source === 'websocket.reconnect' ? undefined : 'err')
    }, [emitClientLog, log]),
  })
  useEffect(() => {
    sendWsRef.current = ws.sendWs
  }, [ws.sendWs])

  useEffect(() => {
    const onError = (event: ErrorEvent) => {
      emitClientLog('error', 'window.onerror', event.message, {
        filename: event.filename,
        lineno: event.lineno,
        colno: event.colno,
        stack: event.error instanceof Error ? event.error.stack : undefined,
      })
    }
    const onUnhandledRejection = (event: PromiseRejectionEvent) => {
      const reason = event.reason
      emitClientLog('error', 'window.unhandledrejection', reason instanceof Error ? reason.message : String(reason), {
        stack: reason instanceof Error ? reason.stack : undefined,
      })
    }
    window.addEventListener('error', onError)
    window.addEventListener('unhandledrejection', onUnhandledRejection)
    return () => {
      window.removeEventListener('error', onError)
      window.removeEventListener('unhandledrejection', onUnhandledRejection)
    }
  }, [emitClientLog])

  // ── Chunk playback ─────────────────────────────────────────────
  const playback = useChunkPlayback(speakCvsRef, {
    setMode,
    log,
    onAllChunksDone: useCallback(() => {
      setMode('idle')
      setIsBusy(false)
      isBusyRef.current = false  // Sync so VAD onSpeechStart sees ready state immediately
      log('ready', 'ok')
    }, [log]),
    onFirstFrameRender: useCallback((chunk: number, turnId?: string) => {
      ws.sendWs({ type: 'client_first_render', chunk, turn_id: turnId })
    }, [ws]),
    // NOTE: we intentionally do NOT dispatch per-chunk React state here. With ~20 segments
    // per answer, dispatching on every chunk start/end forced a top-level re-render at each
    // boundary — the exact main-thread hitch behind the boundary frame stutter — and the
    // state it set (activeSpokenChunk) was never read anywhere. The render loop drives the
    // canvas imperatively; it must not be coupled to React renders.
  })
  const playbackRef = useRef(playback)

  useEffect(() => {
    stopPlaybackRef.current = playback.stopPlayback
    playbackRef.current = playback
  }, [playback])

  // Single source of truth for starting the intro: invoked directly from the
  // tap-to-start overlay's onClick, so it always runs in a genuine user-gesture
  // context (audio unlocked). The overlay auto-shows on every page load.
  const startIntro = useCallback(() => {
    userInteractedRef.current = true
    try { playback.ensureAudioContext() } catch { /* never let an audio hiccup block the video */ }
    setAwaitingIntroTap(false)
    const url = pendingIntroUrlRef.current ?? lastIntroUrlRef.current
    pendingIntroUrlRef.current = null
    if (url) playIntroVideo(url)
    // Begin active listening WHILE the intro plays (not after) so the user can speak /
    // barge in immediately. This same tap is the user gesture that lets getUserMedia +
    // the VAD start. ensureActiveListening no-ops if the mic is muted or already active.
    void micRef.current.ensureActiveListening()
  }, [playback, playIntroVideo])

  // ── VAD bars ───────────────────────────────────────────────────
  const updateVAD = useCallback((level: number) => {
    const holder = vadHolderRef.current
    if (!holder) return
    const bars = Array.from(holder.querySelectorAll('.vb')) as HTMLDivElement[]
    bars.forEach((b, i) => {
      const c = (bars.length - 1) / 2
      const d = Math.abs(i - c) / c
      const visible = isListeningRef.current || activeListeningRef.current
      const h = visible ? Math.max(3, level * (1 - d * 0.55) * 18) : 3
      b.style.height = `${h}px`
      b.style.opacity = visible ? String(0.25 + level * 0.6) : '0'
    })
  }, [])

  // ── Microphone ─────────────────────────────────────────────────
  const micRef = useRef<{
    toggleMic: () => Promise<void>
    stopMic: () => void
    toggleActiveListening: () => Promise<void>
    ensureActiveListening: () => Promise<void>
    isListeningRef: React.RefObject<boolean>
  }>(null!)
  const initMicOnce = useRef(false)

  useEffect(() => {
    if (initMicOnce.current) return
    initMicOnce.current = true

    let activeVad: MicVAD | null = null
    let audioNode: AudioContext | null = null
    let micSource: MediaStreamAudioSourceNode | null = null
    let pcmProcessor: AudioWorkletNode | null = null
    let analyser: AnalyserNode | null = null
    let micStream: MediaStream | null = null
    let vadTimer: number | null = null
    let silenceMs = 0
    let listenStartedAt = 0
    let manualAudioStarted = false
    let activeMode = false
    let activeVadState: ActiveVadState = 'inactive'
    // Pre-roll ring buffer: the VAD only fires onSpeechStart AFTER it has confirmed
    // speech, so the audio that *triggered* detection (the first syllable) is never
    // streamed and the transcript loses the word onset. We continuously buffer the
    // most recent ~preRollMs of frames while idle and flush them to STT the instant
    // speech starts, so Soniox sees the full utterance from the first sound.
    const PREROLL_MAX_SAMPLES = Math.ceil((activeListeningConfig.preRollMs / 1000) * 16000)
    let preRollBuf: Float32Array[] = []
    let preRollSamples = 0

    const setActiveVadState = (state: ActiveVadState) => {
      if (activeVadState === state) return
      activeVadState = state
      if (!activeMode) return
      if (state === 'initializing') log('initializing mic')
      else if (state === 'monitoring') log('listening', 'ok')
      else if (state === 'recording') log('listening')
      else if (state === 'processing') log('processing...')
      else if (state === 'paused') log('paused')
    }

    const readMicLevel = () => {
      if (!analyser) return 0
      const freq = new Uint8Array(analyser.frequencyBinCount)
      analyser.getByteFrequencyData(freq)
      const freqLevel = freq.length ? freq.reduce((a, b) => a + b, 0) / freq.length / 128 : 0
      const time = new Uint8Array(analyser.fftSize)
      analyser.getByteTimeDomainData(time)
      let sumSquares = 0
      for (const sample of time) {
        const centered = (sample - 128) / 128
        sumSquares += centered * centered
      }
      const rmsLevel = time.length ? Math.sqrt(sumSquares / time.length) * 3.2 : 0
      return Math.min(1, Math.max(freqLevel, rmsLevel))
    }

    const closeMicResources = () => {
      if (vadTimer) window.clearInterval(vadTimer)
      vadTimer = null
      if (pcmProcessor) {
        pcmProcessor.port.onmessage = null
        pcmProcessor.disconnect()
      }
      pcmProcessor = null
      micSource?.disconnect()
      micSource = null
      void activeVad?.destroy()
      activeVad = null
      micStream?.getTracks().forEach((t) => t.stop())
      micStream = null
      void audioNode?.close()
      audioNode = null
      analyser = null
      manualAudioStarted = false
      activeVadState = 'inactive'
      updateVAD(0)
    }

    const sendAudioEnd = () => {
      isBusyRef.current = true
      setIsBusy(true)
      sendWsRef.current({ type: 'audio_end' })
    }

    const encodePCM16 = (audio: Float32Array) => {
      const bytes = new Uint8Array(audio.length * 2)
      const view = new DataView(bytes.buffer)
      for (let i = 0; i < audio.length; i += 1) {
        const s = Math.max(-1, Math.min(1, audio[i]))
        view.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true)
      }
      return bytes
    }

    const stopRecording = (sendFinal: boolean) => {
      if (!isListeningRef.current) return
      isListeningRef.current = false
      setIsListening(false)
      if (activeMode) {
        setActiveVadState('monitoring')
        setMode('idle')
        setPartialText('')
        silenceMs = 0
        return
      }
      const shouldSend = sendFinal
      if (shouldSend && manualAudioStarted) {
        sendAudioEnd()
        setMode('thinking')
        log('processing...')
      } else if (shouldSend) {
        setMode('idle')
        log('could not hear - try again', 'err')
      } else if (activeMode) {
        setMode('idle')
        log('active listening', 'ok')
      }
      setPartialText('')
      silenceMs = 0
    }

    const stopMic = () => {
      stopRecording(false)
      activeMode = false
      setActiveListening(false)
      closeMicResources()
      setMode('idle')
      log('ready', 'ok')
    }

    const beginRecording = () => {
      if (!micStream || isListeningRef.current || isBusyRef.current) return
      manualAudioStarted = false
      silenceMs = 0
      listenStartedAt = Date.now()
      setIsListening(true)
      setIsBusy(false)
      if (activeMode) setActiveVadState('recording')
      setMode('listening')
      log('listening')
    }

    const startMic = async () => {
      playback.ensureAudioContext()
      try {
        activeMode = false
        setActiveListening(false)
        micStream = await navigator.mediaDevices.getUserMedia({
          audio: { sampleRate: 16000, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        })
        audioNode = new AudioContext({ sampleRate: 16000 })
        analyser = audioNode.createAnalyser()
        analyser.fftSize = 64
        micSource = audioNode.createMediaStreamSource(micStream)
        micSource.connect(analyser)
        await audioNode.audioWorklet.addModule('/pcm-worklet.js')
        pcmProcessor = new AudioWorkletNode(audioNode, 'pcm-capture', {
          numberOfInputs: 1,
          numberOfOutputs: 1,
          outputChannelCount: [1],
          processorOptions: {
            targetSampleRate: 16000,
            frameSamples: 480,
          },
        })
        pcmProcessor.port.onmessage = (event: MessageEvent<ArrayBuffer>) => {
          if (!isListeningRef.current || activeMode) return
          const frame = new Float32Array(event.data)
          manualAudioStarted = true
          sendWsRef.current({ type: 'audio_chunk', data: encodeBase64(encodePCM16(frame)) })
        }
        micSource.connect(pcmProcessor)
        pcmProcessor.connect(audioNode.destination)

        vadTimer = window.setInterval(() => {
          const level = readMicLevel()
          updateVAD(level)
          if (!isListeningRef.current) return
          if (level < VAD_SILENCE_LEVEL) silenceMs += VAD_INTERVAL_MS
          else silenceMs = 0
          if (Date.now() - listenStartedAt > MIN_RECORD_MS && silenceMs >= AUTO_ENDPOINT_MS) {
            stopRecording(true)
            closeMicResources()
          }
        }, VAD_INTERVAL_MS)

        sendWsRef.current({ type: 'prepare_stt' })
        beginRecording()
      } catch (e) {
        closeMicResources()
        const err = e as { name?: string; message?: string }
        if (err.name === 'NotAllowedError' || err.name === 'PermissionDeniedError') {
          log('Microphone access denied - allow mic permission and refresh', 'err')
        } else {
          log(`mic: ${err.message ?? 'unknown error'}`, 'err')
        }
      }
    }

    const toggleMic = async () => {
      idleTimerRef.current.reset()
      if (isListeningRef.current) {
        stopRecording(true)
        closeMicResources()
        return
      }
      await startMic()
    }

    const startActiveListening = async () => {
      if (!activeListeningConfig.enabled) return
      playback.ensureAudioContext()
      try {
        closeMicResources()
        pendingActiveListeningRef.current = false
        activeMode = true
        setActiveListening(true)
        setActiveVadState('initializing')

        // Direct audio streaming: send every VAD frame to Soniox immediately.
        // No buffering — Soniox processes audio incrementally and needs audio
        // ASAP to start transcribing. The VAD library provides ~30ms frames
        // at 16kHz/16bit mono, which is exactly what Soniox expects.
        const vad = await MicVAD.new({
          model: activeListeningConfig.model,
          baseAssetPath: '/vendor/vad/',
          onnxWASMBasePath: './',
          ortConfig: (ort) => {
            ort.env.logLevel = 'error'
            const ortBaseUrl = new URL('/vendor/onnxruntime/', window.location.origin).href
            ort.env.wasm.wasmPaths = {
              mjs: `${ortBaseUrl}ort-wasm-simd-threaded.mjs`,
              wasm: `${ortBaseUrl}ort-wasm-simd-threaded.wasm`,
            }
          },
          positiveSpeechThreshold: activeListeningConfig.positiveSpeechThreshold,
          negativeSpeechThreshold: activeListeningConfig.negativeSpeechThreshold,
          redemptionMs: activeListeningConfig.redemptionMs,
          preSpeechPadMs: activeListeningConfig.preRollMs,
          minSpeechMs: activeListeningConfig.minSpeechMs,
          startOnLoad: false,
          submitUserSpeechOnPause: false,
          getStream: () => navigator.mediaDevices.getUserMedia({
            audio: { sampleRate: 16000, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
          }),
          onSpeechStart: () => {
            if (!micEnabledRef.current) return
            sendWsRef.current({ type: 'prepare_stt' })
            isListeningRef.current = true
            setIsListening(true)
            setActiveVadState('recording')
            setMode('listening')
            // Flush the buffered onset so the transcript isn't missing the first word.
            if (preRollBuf.length > 0) {
              for (const f of preRollBuf) {
                sendWsRef.current({ type: 'audio_chunk', data: encodeBase64(encodePCM16(f)) })
              }
              preRollBuf = []
              preRollSamples = 0
            }
          },
          onSpeechRealStart: () => {},
          onVADMisfire: () => {
            isListeningRef.current = false
            setIsListening(false)
            setActiveVadState(isBusyRef.current ? 'paused' : 'monitoring')
            setMode('idle')
          },
          onFrameProcessed: (probabilities, frame) => {
            if (!micEnabledRef.current) { updateVAD(0); return }
            updateVAD(probabilities.isSpeech)
            if (isListeningRef.current) {
              // Send every frame immediately — Soniox processes streaming audio
              // incrementally. Any delay here adds directly to time-to-first-partial.
              const pcm16 = encodePCM16(frame)
              sendWsRef.current({ type: 'audio_chunk', data: encodeBase64(pcm16) })
            } else {
              // Idle: keep the last ~preRollMs of audio so onSpeechStart can flush the
              // onset. Copy the frame — the VAD reuses its buffer between callbacks.
              preRollBuf.push(frame.slice())
              preRollSamples += frame.length
              while (preRollSamples > PREROLL_MAX_SAMPLES && preRollBuf.length > 1) {
                preRollSamples -= preRollBuf.shift()!.length
              }
              if (activeVadState !== 'monitoring') setActiveVadState('monitoring')
            }
          },
          onSpeechEnd: () => {
            if (!micEnabledRef.current) return
            isListeningRef.current = false
            setIsListening(false)
            if (isBusyRef.current || playbackRef.current.isPlayingRef.current) {
              sendAudioEnd()
              return
            }
            setActiveVadState('processing')
            setMode('thinking')
            setPartialText('')
            sendAudioEnd()
          },
        } satisfies Partial<RealTimeVADOptions>)
        activeVad = vad
        await vad.start()
        setActiveVadState('monitoring')
        setMode('idle')
      } catch (e) {
        closeMicResources()
        const err = e as { name?: string; message?: string }
        setActiveListening(false)
        activeMode = false
        activeVad = null
        if (err.name === 'NotAllowedError' || err.name === 'PermissionDeniedError') {
          log('Microphone access denied - allow mic permission and refresh', 'err')
        } else {
          log(`mic: ${err.message ?? 'unknown error'}`, 'err')
        }
      }
    }

    const toggleActiveListening = async () => {
      idleTimerRef.current.reset()
      if (activeMode || activeListeningRef.current) {
        stopMic()
        return
      }
      await startActiveListening()
    }

    const ensureActiveListening = async () => {
      if (!micEnabledRef.current || activeMode || activeListeningRef.current) return
      await startActiveListening()
    }

    micRef.current = { toggleMic, stopMic, toggleActiveListening, ensureActiveListening, isListeningRef }
  }, [log, updateVAD, playback])

  // ── Idle timer ─────────────────────────────────────────────────
  // Idle timeout: stop any active mic/playback but preserve conversation history.
  // The user can manually reset via the Settings → "Reset session" button.
  const sessionReset = useCallback(() => {
    if (isListeningRef.current || activeListeningRef.current) micRef.current.stopMic()
    if (isBusyRef.current) {
      sendWsRef.current({ type: 'interrupt' })
      activeTurnIdRef.current = null
      stopPlaybackRef.current()
    }
    setMode('idle')
    setIsBusy(false)
    setPartialText('')
    log('')
    idleTimerRef.current.reset()
  }, [log])

  const resetBackendSession = useCallback(() => {
    sessionReset()
    activeTurnIdRef.current = null
    dispatchConversation({ type: 'reset' })
    sendWsRef.current({ type: 'reset' })
  }, [sessionReset])

  const idleTimer = useIdleTimer(IDLE_TIMEOUT_MS, sessionReset)

  useEffect(() => {
    idleTimerRef.current = idleTimer
  }, [idleTimer])

  // Persistent gesture listener that unlocks audio on any tap/key and, when idle, starts
  // active listening. The intro itself is started by the tap-to-start overlay's own
  // onClick (see startIntro) so it runs in a guaranteed user-gesture context; this
  // listener only marks that the user has interacted and never touches the intro.
  useEffect(() => {
    const onGesture = () => {
      userInteractedRef.current = true
      playback.ensureAudioContext()
      if (awaitingIntroTapRef.current) return  // the overlay button handles the intro
      if (!isBusyRef.current && micEnabledRef.current && !activeListeningRef.current && !isListeningRef.current) {
        void micRef.current.ensureActiveListening()
      }
    }
    window.addEventListener('pointerdown', onGesture)
    window.addEventListener('keydown', onGesture)
    return () => {
      window.removeEventListener('pointerdown', onGesture)
      window.removeEventListener('keydown', onGesture)
    }
  }, [playback])

  // ── Keyboard shortcuts ───────────────────────────────────────
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return
      if (e.code === 'Space') {
        e.preventDefault()
        void micRef.current.toggleMic()
      }
      if (e.code === 'Escape') {
        e.preventDefault()
        sendWsRef.current({ type: 'interrupt' })
        activeTurnIdRef.current = null
        stopPlaybackRef.current()
        setMode('idle')
        setIsBusy(false)
        setStatusText('ready')
        dispatchConversation({ type: 'interrupted' })
        log('')
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [log, playback])

  // ── Side effects ─────────────────────────────────────────────
  useEffect(() => {
    idleVidRef.current?.play().catch(() => {})
  }, [])

  useEffect(() => {
    document.documentElement.dataset.theme = darkMode ? 'dark' : 'light'
  }, [darkMode])

  useEffect(() => {
    if (!connectedAt) return
    const update = () => setConnectedSeconds(Math.max(0, Math.floor((Date.now() - connectedAt) / 1000)))
    update()
    const timer = window.setInterval(update, 1000)
    return () => window.clearInterval(timer)
  }, [connectedAt])

  // ── Text send ──────────────────────────────────────────────────
  const sendText = () => {
    const text = inputText.trim()
    if (!text) return
    submitPrompt(text)
  }

  const submitPrompt = useCallback((text: string) => {
    idleTimerRef.current.reset()
    playback.ensureAudioContext()
    // Typing a question counts as the first interaction — drop any pending intro gate.
    userInteractedRef.current = true
    pendingIntroUrlRef.current = null
    setAwaitingIntroTap(false)
    if (!text) return
    if (isBusyRef.current) {
      log('wait for the current response to finish', 'err')
      return
    }
    if (!isSocketOpenRef.current) {
      pendingPromptRef.current = text
      flushPendingPrompt()
      log('connecting websocket... retrying automatically')
      if (reconnectPromptRetryRef.current != null) {
        window.clearTimeout(reconnectPromptRetryRef.current)
      }
      reconnectPromptRetryRef.current = window.setTimeout(() => {
        flushPendingPrompt()
        if (pendingPromptRef.current) {
          log('websocket not connected - refresh the page', 'err')
        }
      }, 1200)
      return
    }
    pendingPromptRef.current = null
    sendTextPrompt(text)
  }, [log, playback, sendTextPrompt, flushPendingPrompt])

  useEffect(() => {
    return () => {
      if (reconnectPromptRetryRef.current != null) {
        window.clearTimeout(reconnectPromptRetryRef.current)
      }
    }
  }, [])

  const interrupt = () => {
    sendWsRef.current({ type: 'interrupt' })
    activeTurnIdRef.current = null
    stopPlaybackRef.current()
    stopIntroVideo()
    dispatchConversation({ type: 'interrupted' })
    setIsBusy(false)
    setMode('idle')
    setStatusText('ready')
    log('interrupted')
  }

  // ── Render ─────────────────────────────────────────────────────
  return (
    <div className={`app ${showComposer ? 'composer-open' : ''}`}>
      {busyWaiting && (
        <div className="busy-overlay" role="status" aria-live="polite">
          <div className="busy-card">
            <span className="busy-spinner" aria-hidden="true" />
            <h2>The avatar is currently in use</h2>
            <p>Someone else is talking with the demo right now. You'll connect automatically as soon as it's free.</p>
          </div>
        </div>
      )}
      <header className="app-header">
        <div className="brand-group">
          <h1 className="brand-title">AIFC</h1>
        </div>
        <div className="header-actions">
          <button
            className={`icon-btn panel-toggle ${showLeftPanel ? 'active' : ''}`}
            type="button"
            onClick={() => setShowLeftPanel((v) => !v)}
            aria-pressed={showLeftPanel}
            aria-label={showLeftPanel ? 'Collapse stream panel' : 'Expand stream panel'}
          >
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <rect x="3" y="4" width="18" height="16" rx="2" />
              <path d="M9 4v16" />
            </svg>
          </button>
          <button
            className={`icon-btn panel-toggle right ${showRightPanel ? 'active' : ''}`}
            type="button"
            onClick={() => setShowRightPanel((v) => !v)}
            aria-pressed={showRightPanel}
            aria-label={showRightPanel ? 'Collapse assistant panel' : 'Expand assistant panel'}
          >
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <rect x="3" y="4" width="18" height="16" rx="2" />
              <path d="M15 4v16" />
            </svg>
          </button>
          <button
            className={`icon-btn ${showSettings ? 'active' : ''}`}
            type="button"
            onClick={() => setShowSettings((v) => !v)}
            aria-expanded={showSettings}
            aria-label={showSettings ? 'Close settings' : 'Open settings'}
          >
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7z" />
              <path d="M19.4 15a1.8 1.8 0 0 0 .36 1.98l.05.05a2.1 2.1 0 0 1-2.97 2.97l-.05-.05a1.8 1.8 0 0 0-1.98-.36 1.8 1.8 0 0 0-1.08 1.65V21a2.1 2.1 0 0 1-4.2 0v-.08a1.8 1.8 0 0 0-1.08-1.65 1.8 1.8 0 0 0-1.98.36l-.05.05a2.1 2.1 0 0 1-2.97-2.97l.05-.05A1.8 1.8 0 0 0 3.6 15a1.8 1.8 0 0 0-1.65-1.08H1.9a2.1 2.1 0 0 1 0-4.2h.08A1.8 1.8 0 0 0 3.63 8.64a1.8 1.8 0 0 0-.36-1.98l-.05-.05a2.1 2.1 0 0 1 2.97-2.97l.05.05a1.8 1.8 0 0 0 1.98.36h.01A1.8 1.8 0 0 0 9.3 2.4V2.1a2.1 2.1 0 0 1 4.2 0v.08a1.8 1.8 0 0 0 1.08 1.65 1.8 1.8 0 0 0 1.98-.36l.05-.05a2.1 2.1 0 0 1 2.97 2.97l-.05.05a1.8 1.8 0 0 0-.36 1.98v.01a1.8 1.8 0 0 0 1.65 1.08h.08a2.1 2.1 0 0 1 0 4.2h-.08A1.8 1.8 0 0 0 19.4 15z" />
            </svg>
          </button>
          <ThemeToggle darkMode={darkMode} onToggle={() => setDarkMode((d) => !d)} />
        </div>
        {showSettings && (
          <div className="settings-popover" role="dialog" aria-label="Interface settings">
            <div className="settings-row">
              <span>Light mode</span>
              <button className={`mini-toggle ${!darkMode ? 'on' : ''}`} onClick={() => setDarkMode((d) => !d)} aria-pressed={!darkMode}>
                <span />
              </button>
            </div>
            <div className="settings-row">
              <span>Active listening</span>
              <button className={`mini-toggle ${activeListening ? 'on' : ''}`} onClick={() => { void micRef.current.toggleActiveListening() }} aria-pressed={activeListening}>
                <span />
              </button>
            </div>
            <div className="settings-row">
              <span>Text composer</span>
              <button className={`mini-toggle ${showComposer ? 'on' : ''}`} onClick={() => setShowComposer((v) => !v)} aria-pressed={showComposer}>
                <span />
              </button>
            </div>
            <div className="settings-row">
              <span>Stream panel</span>
              <button className={`mini-toggle ${showLeftPanel ? 'on' : ''}`} onClick={() => setShowLeftPanel((v) => !v)} aria-pressed={showLeftPanel}>
                <span />
              </button>
            </div>
            <div className="settings-row">
              <span>Assistant panel</span>
              <button className={`mini-toggle ${showRightPanel ? 'on' : ''}`} onClick={() => setShowRightPanel((v) => !v)} aria-pressed={showRightPanel}>
                <span />
              </button>
            </div>
            <button className="settings-action" type="button" onClick={resetBackendSession}>Reset session</button>
          </div>
        )}
      </header>
      <main className="main">
        <section className={`main-grid ${!showLeftPanel ? 'left-collapsed' : ''} ${!showRightPanel ? 'right-collapsed' : ''}`} aria-label="AI avatar call screen">
          {showLeftPanel && (
          <SidebarCard title="Stream Status" className="left-panel">
            <div className="panel-section-title">Connection</div>
            <div className="stream-monitor-card">
              <div className="stream-live-row">
                <strong><span /> Live</strong>
                <em>{connectedAt ? connectionTime : '00:00'}</em>
              </div>
              <div className="stream-health-grid" aria-label="Stream health">
                <div className={`health-tile ${connectedAt ? 'ok' : 'warn'}`}>
                  <span>Connection</span>
                  <strong>{connectedAt ? 'Connected' : 'Reconnecting'}</strong>
                </div>
                <div className="health-tile">
                  <span>Last answer</span>
                  <strong>{lastLatencyMs != null ? `${(lastLatencyMs / 1000).toFixed(1)}s` : '—'}</strong>
                </div>
              </div>
            </div>

            <div className="panel-metric-block stream-signal-block">
              <div className="panel-metric-head">
                <span>Stream signal</span>
                <strong>Excellent</strong>
              </div>
              <div className="audio-meter stream-meter" aria-hidden="true">
                {Array.from({ length: 24 }, (_, i) => <span key={i} className="active" />)}
              </div>
            </div>

            <div className="panel-metric-block audio-level-block">
              <div className="panel-metric-head">
                <span>Audio level</span>
                <strong>{micEnabled ? 'Good' : 'Muted'}</strong>
              </div>
              <div className="audio-level-inline">
                <div className="audio-meter compact-meter" aria-hidden="true">
                  {Array.from({ length: 12 }, (_, i) => <span key={i} className={i < 9 ? 'active' : ''} />)}
                </div>
              </div>
            </div>

            <div className="panel-section-title">Session controls</div>
            <div className="session-control-list" aria-label="Session controls">
              <button type="button" onClick={() => { void micRef.current.toggleMic() }}>
                <span>Push to talk</span>
                <strong>Space</strong>
              </button>
              <button type="button" onClick={() => { void micRef.current.toggleActiveListening() }}>
                <span>{activeListening ? 'Pause listening' : 'Start listening'}</span>
                <strong>Auto</strong>
              </button>
              <button type="button" onClick={() => {
                const enabled = !micEnabled
                setMicEnabled(enabled)
                if (!enabled && (isListeningRef.current || activeListeningRef.current)) micRef.current.stopMic()
                if (enabled) void micRef.current.ensureActiveListening()
              }}>
                <span>{micEnabled ? 'Mute microphone' : 'Enable microphone'}</span>
                <strong>M</strong>
              </button>
            </div>
          </SidebarCard>
          )}

          <div className="center-col">
            <div className={`stage-stack ${showComposer ? 'composer-visible' : ''}`} ref={stageStackRef}>
              <AvatarStage
                fullscreenTargetRef={stageStackRef}
                idleVideoRef={idleVidRef}
                introVideoRef={introVidRef}
                introActive={introActive}
                awaitingIntroTap={awaitingIntroTap}
                onStartIntro={startIntro}
                speakCanvasRef={speakCvsRef}
                mode={mode}
                micEnabled={micEnabled}
                activeListening={activeListening}
                isListening={isListening}
                isBusy={isBusy}
                showComposer={showComposer}
                showTranscript={Boolean(isListening || partialText)}
                onTalk={() => {
                  // "Tap to talk": ensure the mic is on (unmute if needed) and listening.
                  if (!micEnabledRef.current) { micEnabledRef.current = true; setMicEnabled(true) }
                  void micRef.current.ensureActiveListening()
                }}
                onToggleMute={() => {
                  const enabled = !micEnabled
                  setMicEnabled(enabled)
                  if (!enabled && (isListeningRef.current || activeListeningRef.current)) micRef.current.stopMic()
                  if (enabled) void micRef.current.ensureActiveListening()
                }}
                onInterrupt={interrupt}
                onToggleComposer={() => {
                  setShowChat(true)
                  setShowComposer((v) => !v)
                }}
              />

              {/* stage-meta is always rendered so it reserves space and the stage never resizes.
                  It is visually hidden (visibility:hidden) when there is nothing to show. */}
              <div className={`stage-meta ${isListening ? 'listening' : ''} ${!isListening && !partialText ? 'stage-meta-hidden' : ''}`}>
                  <div className="partial" aria-live="polite">
                    <span className="partial-placeholder">
                      {isListening ? 'Listening' : 'Transcript'}
                    </span>
                    <span className="partial-text">{partialText}</span>
                  </div>
                  <div className="vad-wrap" aria-hidden="true">
                    <div className="vad" ref={vadHolderRef}>
                      {Array.from({ length: 17 }, (_, i) => (
                        <div key={i} className="vb" />
                      ))}
                    </div>
                  </div>
                </div>

              <FloatingChatComposer
                open={showComposer}
                value={inputText}
                placeholder={isListening ? 'Listening...' : isBusy ? 'Assistant is responding...' : 'Ask me anything…'}
                busy={isBusy}
                onChange={(text) => setInputText(text)}
                onSubmit={sendText}
                onClose={() => setShowComposer(false)}
              />
            </div>
          </div>

          {showRightPanel && (
          <SidebarCard title="AI Assistant" className="right-panel">
            {showChat && (
              <ChatPanel
                messages={messages}
                partialText={partialText}
                isListening={isListening}
                aiMessageId={aiMessageId}
              />
            )}
          </SidebarCard>
          )}
        </section>
      </main>

      <StatusBar logText={logText} logClass={logClass} />
    </div>
  )
}
