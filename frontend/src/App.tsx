
import { useState, useEffect, useRef, useCallback, useReducer } from 'react'
import type { ReactNode } from 'react'
import type { WsInbound, UiMode, StructuredAnswer } from './types'
import { encodeBase64 } from './utils'
import { MicVAD, utils as vadUtils } from '@ricky0123/vad-web'
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
import { StatusBar } from './components/StatusBar'
import { ThemeToggle } from './components/StatusIndicator'
import { conversationReducer, initialConversationState } from './state/conversationReducer'
import './styles.css'

function detectUiLanguage(text: string): 'en' | 'ru' | 'kk' | 'zh' {
  const lower = text.toLowerCase()
  if (/[\u4e00-\u9fff]/.test(lower)) return 'zh'
  if (/[әғқңөұүһі]/.test(lower) || /(сәлем|рахмет|рақмет|қалай|жоқ|иә|жұмыс|құжат)/.test(lower)) return 'kk'
  if (/[а-яё]/.test(lower)) return 'ru'
  return 'en'
}

function SidebarCard({ title, children, className = '' }: { title: string; children: ReactNode; className?: string }) {
  return (
    <aside className={`sidebar-card ${className}`} aria-label={title}>
      <div className="sidebar-card-hdr">
        <span>{title}</span>
        <span className="panel-dot" aria-hidden="true" />
      </div>
      <div className="sidebar-card-body">{children}</div>
    </aside>
  )
}

type ActiveVadState = 'inactive' | 'initializing' | 'monitoring' | 'recording' | 'processing' | 'paused'

export default function App() {
  const [mode, setMode] = useState<UiMode | string>('idle')
  const [, setStatusText] = useState('connecting')
  const [conversation, dispatchConversation] = useReducer(conversationReducer, initialConversationState)
  const messages = conversation.messages
  const aiMessageId = conversation.aiMessageId
  const stageFollowUps = conversation.stageFollowUps
  const showStageFollowUps = conversation.showStageFollowUps
  const [partialText, setPartialText] = useState('')
  const [inputText, setInputText] = useState('')
  const [isBusy, setIsBusy] = useState(false)
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

  // ── Refs ──────────────────────────────────────────────────────
  const idleVidRef = useRef<HTMLVideoElement | null>(null)
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
  const stageFollowUpsRef = useRef<string[]>([])
  const showChatRef = useRef(false)
  const idleTimerRef = useRef<{ reset: () => void; clear: () => void }>({ reset: () => {}, clear: () => {} })

  useEffect(() => {
    isBusyRef.current = isBusy
    isListeningRef.current = isListening
    activeListeningRef.current = activeListening
    micEnabledRef.current = micEnabled
    sttReadyRef.current = sttReady
  }, [isBusy, isListening, activeListening, micEnabled, sttReady])

  useEffect(() => {
    stageFollowUpsRef.current = stageFollowUps
  }, [stageFollowUps])

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

  const formatStructuredDetails = useCallback((answer: StructuredAnswer) => {
    const blocks: string[] = []
    const seen = new Set<string>()
    const pushBlock = (raw: string) => {
      const value = raw.trim()
      if (!value) return
      const key = value.replace(/\s+/g, ' ').trim().toLowerCase()
      if (seen.has(key)) return
      seen.add(key)
      blocks.push(value)
    }
    const summary = answer.details.summary?.trim()
    if (summary) pushBlock(summary)
    for (const section of answer.details.sections) {
      const title = section.title?.trim()
      const text = section.text?.trim()
      const items = (section.items ?? []).map((item) => item.trim()).filter(Boolean)
      const sectionParts: string[] = []
      const hasBody = Boolean(text && text !== summary) || items.length > 0
      if (title && title.toLowerCase() !== 'details' && hasBody) sectionParts.push(`### ${title}`)
      if (items.length > 0) sectionParts.push(items.map((item) => `- ${item}`).join('\n'))
      if (text && text !== summary) sectionParts.push(text)
      if (sectionParts.length > 0) pushBlock(sectionParts.join('\n'))
    }
    return blocks.join('\n\n').trim() || answer.spoken
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
    onMessage: (msg: WsInbound) => {
      switch (msg.type) {
        case 'session_state':
          currentSessionIdRef.current = msg.session_id
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
            details: msg.details,
            key_points: msg.key_points,
            follow_up_questions: msg.follow_up_questions,
            }
            dispatchConversation({
              type: 'answer_payload',
              answer: nextAnswer,
              formattedText: formatStructuredDetails(nextAnswer),
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
          playbackRef.current.onAudioReady(chunk, msg.data, msg.frame_stride ?? 1, msg.turn_id, Boolean(msg.cached))
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
          log(`${msg.chunks ?? 1} chunk(s)`, 'ok')
          emitClientLog('info', 'pipeline.done', 'turn completed', { turnId: msg.turn_id, latencyMs: msg.latency_ms })
          playbackRef.current.onAllDone(msg.chunks ?? 1)
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
          isBusyRef.current = false
          setIsBusy(false)
          setMode('idle')
          setPartialText('')
          log('stopped', 'ok')
          break
        case 'interrupted':
          activeTurnIdRef.current = null
          playbackRef.current.stopPlayback()
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
      dispatchConversation({ type: 'active_spoken_chunk', chunk: null })
      setMode('idle')
      setIsBusy(false)
      isBusyRef.current = false  // Sync so VAD onSpeechStart sees ready state immediately
      if (stageFollowUpsRef.current.length > 0) dispatchConversation({ type: 'show_followups' })
      log('ready', 'ok')
    }, [log]),
    onFirstFrameRender: useCallback((chunk: number, turnId?: string) => {
      ws.sendWs({ type: 'client_first_render', chunk, turn_id: turnId })
    }, [ws]),
    onChunkPlaybackStart: useCallback((chunk: number) => {
      dispatchConversation({ type: 'active_spoken_chunk', chunk })
    }, []),
    onChunkPlaybackEnd: useCallback(() => {
      dispatchConversation({ type: 'active_spoken_chunk', chunk: null })
    }, []),
  })
  const playbackRef = useRef(playback)

  useEffect(() => {
    stopPlaybackRef.current = playback.stopPlayback
    playbackRef.current = playback
  }, [playback])

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
    let manualAudioFrames: Float32Array[] = []
    let activeMode = false
    let activeVadState: ActiveVadState = 'inactive'

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
      manualAudioFrames = []
      activeVadState = 'inactive'
      updateVAD(0)
    }

    const sendVadAudio = async (audio: Float32Array) => {
      if (!audio.length) return
      isBusyRef.current = true
      setIsBusy(true)
      const wav = vadUtils.encodeWAV(audio, 1, 16000, 1, 16)
      sendWsRef.current({ type: 'audio', data: encodeBase64(new Uint8Array(wav)) })
    }

    const mergeAudioFrames = (frames: Float32Array[]) => {
      const totalLength = frames.reduce((sum, frame) => sum + frame.length, 0)
      const merged = new Float32Array(totalLength)
      let offset = 0
      for (const frame of frames) {
        merged.set(frame, offset)
        offset += frame.length
      }
      return merged
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
      const framesToSend = manualAudioFrames.slice()
      manualAudioFrames = []
      if (activeMode) {
        setActiveVadState('monitoring')
        setMode('idle')
        setPartialText('')
        silenceMs = 0
        return
      }
      const shouldSend = sendFinal
      if (shouldSend && framesToSend.length) {
        void sendVadAudio(mergeAudioFrames(framesToSend))
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
      manualAudioFrames = []
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
          manualAudioFrames.push(frame)
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
            }
            if (!isListeningRef.current && activeVadState !== 'monitoring') setActiveVadState('monitoring')
          },
          onSpeechEnd: (audio) => {
            if (!micEnabledRef.current) return
            isListeningRef.current = false
            setIsListening(false)
            if (isBusyRef.current || playbackRef.current.isPlayingRef.current) {
              // During TTS playback: send the final audio to finalize the Soniox stream.
              // The frontend VAD detects silence faster (400ms AUTO_ENDPOINT_MS) than
              // Soniox's endpoint delay (500ms), so this path handles stop keyword
              // detection via the "audio" handler directly — which is faster.
              const wav = vadUtils.encodeWAV(audio, 1, 16000, 1, 16)
              sendWsRef.current({ type: 'audio', data: encodeBase64(new Uint8Array(wav)) })
              return
            }
            setActiveVadState('processing')
            setMode('thinking')
            setPartialText('')
            void sendVadAudio(audio)
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

  useEffect(() => {
    if (!micEnabled || activeListening || isListening || isBusy) return
    const start = () => {
      if (!micEnabledRef.current || activeListeningRef.current || isListeningRef.current) return
      void micRef.current.ensureActiveListening()
    }
    window.addEventListener('pointerdown', start, { once: true })
    window.addEventListener('keydown', start, { once: true })
    return () => {
      window.removeEventListener('pointerdown', start)
      window.removeEventListener('keydown', start)
    }
  }, [micEnabled, activeListening, isListening, isBusy])

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
    dispatchConversation({ type: 'interrupted' })
    setIsBusy(false)
    setMode('idle')
    setStatusText('ready')
    log('interrupted')
  }

  // ── Render ─────────────────────────────────────────────────────
  return (
    <div className={`app ${showComposer ? 'composer-open' : ''}`}>
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
                <div className="health-tile">
                  <span>Health</span>
                  <strong>Excellent</strong>
                </div>
                <div className="health-tile">
                  <span>Latency</span>
                  <strong>Low</strong>
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
                speakCanvasRef={speakCvsRef}
                mode={mode}
                micEnabled={micEnabled}
                activeListening={activeListening}
                isListening={isListening}
                isBusy={isBusy}
                showComposer={showComposer}
                followUpQuestions={stageFollowUps}
                showFollowUps={showStageFollowUps}
                showTranscript={Boolean(isListening || partialText)}
                onToggleMic={() => { void micRef.current.toggleMic() }}
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
                onSelectFollowUp={(question) => submitPrompt(question)}
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
