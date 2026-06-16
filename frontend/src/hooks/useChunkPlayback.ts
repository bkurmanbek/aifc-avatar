import { useRef, useCallback, useEffect } from 'react'
import type { ChunkState } from '../types'
import { FPS, CANVAS_W, CANVAS_H } from '../constants'

const LIVE_FRAME_HEADROOM_S = 0.14
const LIVE_READY_FRAME_HEADROOM = 4
const CACHED_READY_FRAME_HEADROOM = 32
const PRELOAD_FRAME_WINDOW = 48
const FRAME_CACHE_INITIAL_LIMIT = 48
const FRAME_CACHE_NEXT_LIMIT = 24
const FRAME_CACHE_FETCH_TIMEOUT_MS = 8000

export interface PlaybackCallbacks {
  setMode: (mode: string) => void
  log: (text: string, cls?: string) => void
  onAllChunksDone: () => void
  onFirstFrameRender?: (chunk: number, turnId?: string) => void
  onChunkPlaybackStart?: (chunk: number) => void
  onChunkPlaybackEnd?: (chunk: number) => void
}

export function useChunkPlayback(
  speakCvsRef: React.RefObject<HTMLCanvasElement | null>,
  callbacks: PlaybackCallbacks,
) {
  const cbRef = useRef(callbacks)

  useEffect(() => {
    cbRef.current = callbacks
  }, [callbacks])

  const acRef = useRef<AudioContext | null>(null)
  const currentSrcRef = useRef<AudioBufferSourceNode | null>(null)
  const renderActiveRef = useRef(false)
  const isPlayingRef = useRef(false)
  const hideSpeakTimerRef = useRef<number | null>(null)
  const chunkGapTimerRef = useRef<number | null>(null)
  const streamActiveRef = useRef(false)
  const firstRenderReportedRef = useRef<Record<string, boolean>>({})
  const playbackSessionRef = useRef(0)
  const activeTurnIdRef = useRef<string | null>(null)
  const frameCacheControllersRef = useRef<Set<AbortController>>(new Set())
  const maybePlayNextRef = useRef<() => void>(() => {})
  const playChunkRef = useRef<(idx: number) => void>(() => {})
  // Chunk transition timing: record when a chunk ends so we can measure the gap to first frame of next chunk
  const chunkEndTsRef = useRef<Record<number, number>>({})

  const chunksRef = useRef<Record<number, ChunkState>>({})
  const nextPlayChunkRef = useRef(0)
  const totalChunksRef = useRef<number>(Infinity)

  const ensureAudioContext = useCallback(() => {
    if (!acRef.current) acRef.current = new AudioContext()
    const ac = acRef.current
    if (ac.state === 'suspended') ac.resume().catch(() => {})
    try {
      const silent = ac.createBuffer(1, 1, 22050)
      const src = ac.createBufferSource()
      src.buffer = silent
      src.connect(ac.destination)
      src.start(0)
    } catch {
      // ignore
    }
  }, [])

  const showSpeak = useCallback(() => {
    const c = speakCvsRef.current
    if (!c) return
    c.classList.add('show')
    c.style.opacity = '1'
  }, [speakCvsRef])

  const hideSpeak = useCallback(() => {
    const c = speakCvsRef.current
    if (!c) return
    c.style.opacity = '0'
    c.classList.remove('show') // Remove immediately so :has() CSS triggers idle video fade-in at once
    window.setTimeout(() => {
      const ctx = c.getContext('2d')
      ctx?.clearRect(0, 0, CANVAS_W, CANVAS_H)
    }, 200)
  }, [speakCvsRef])

  const scheduleHideSpeak = useCallback((delayMs = 180) => {
    if (hideSpeakTimerRef.current) window.clearTimeout(hideSpeakTimerRef.current)
    hideSpeakTimerRef.current = window.setTimeout(() => {
      if (!isPlayingRef.current) hideSpeak()
    }, delayMs)
  }, [hideSpeak])

  const stopPlayback = useCallback(() => {
    playbackSessionRef.current += 1
    renderActiveRef.current = false
    isPlayingRef.current = false
    streamActiveRef.current = false
    // Reset perf log on new session so stale data doesn't accumulate
    ;(window as Record<string, unknown>).__avatarPerf = []
    if (hideSpeakTimerRef.current) window.clearTimeout(hideSpeakTimerRef.current)
    if (chunkGapTimerRef.current) window.clearTimeout(chunkGapTimerRef.current)
    if (currentSrcRef.current) {
      currentSrcRef.current.onended = null
      try { currentSrcRef.current.stop() } catch { /* ignore */ }
      currentSrcRef.current = null
    }
    for (const controller of frameCacheControllersRef.current) controller.abort()
    frameCacheControllersRef.current.clear()
    chunksRef.current = {}
    nextPlayChunkRef.current = 0
    totalChunksRef.current = Infinity
    firstRenderReportedRef.current = {}
    chunkEndTsRef.current = {}
    activeTurnIdRef.current = null
    hideSpeak()
  }, [hideSpeak])

  const isStaleTurn = useCallback((turnId?: string) => {
    return Boolean(turnId && activeTurnIdRef.current !== turnId)
  }, [])

  const ensureChunk = useCallback((idx: number) => {
    if (!chunksRef.current[idx]) chunksRef.current[idx] = { audio: null, frames: [], bitmapCache: {}, bitmapPending: new Set(), frameDone: false, frameStride: 1 }
  }, [])

  // Decode one JPEG frame off-thread via createImageBitmap and cache the GPU bitmap
  const preloadBitmap = useCallback((ch: ChunkState, frameIdx: number) => {
    if (ch.bitmapCache[frameIdx] || ch.bitmapPending.has(frameIdx) || !ch.frames[frameIdx]) return
    ch.bitmapPending.add(frameIdx)
    const img = new Image()
    img.src = `data:image/jpeg;base64,${ch.frames[frameIdx]}`
    createImageBitmap(img).then(bitmap => {
      ch.bitmapPending.delete(frameIdx)
      ch.bitmapCache[frameIdx] = bitmap
    }).catch(() => {
      ch.bitmapPending.delete(frameIdx)
    })
  }, [])

  const isChunkReadyToPlay = useCallback((_idx: number, ch: ChunkState | undefined) => {
    if (!ch?.audio) return false
    if (ch.error) return false
    if (ch.cached) return ch.frames.length >= CACHED_READY_FRAME_HEADROOM || (ch.frameDone && ch.frames.length > 0)
    if (ch.frames.length >= LIVE_READY_FRAME_HEADROOM) return true
    if (ch.frameDone && ch.frames.length > 0) return true
    return false
  }, [])

  const chunkDone = useCallback((idx: number) => {
    renderActiveRef.current = false
    isPlayingRef.current = false
    nextPlayChunkRef.current = idx + 1

    const nextChunk = chunksRef.current[nextPlayChunkRef.current]
    if (isChunkReadyToPlay(nextPlayChunkRef.current, nextChunk)) {
      maybePlayNextRef.current()
      return
    }

    if (nextPlayChunkRef.current >= totalChunksRef.current) {
      cbRef.current.onAllChunksDone()
      if (hideSpeakTimerRef.current) window.clearTimeout(hideSpeakTimerRef.current)
      scheduleHideSpeak(140)
      return
    }

    cbRef.current.setMode(streamActiveRef.current ? 'rendering' : 'speaking')
    if (chunkGapTimerRef.current) window.clearTimeout(chunkGapTimerRef.current)
  }, [isChunkReadyToPlay, scheduleHideSpeak])

  const maybePlayNext = useCallback(() => {
    if (isPlayingRef.current) return
    const ch = chunksRef.current[nextPlayChunkRef.current]
    if (isChunkReadyToPlay(nextPlayChunkRef.current, ch)) {
      if (chunkGapTimerRef.current) window.clearTimeout(chunkGapTimerRef.current)
      playChunkRef.current(nextPlayChunkRef.current)
    }
  }, [isChunkReadyToPlay])

  const playChunk = useCallback(async (idx: number) => {
    const playbackSession = playbackSessionRef.current
    const ch = chunksRef.current[idx]
    if (!ch?.audio) return
    isPlayingRef.current = true
    cbRef.current.onChunkPlaybackStart?.(idx)
    cbRef.current.setMode('speaking')
    if (hideSpeakTimerRef.current) window.clearTimeout(hideSpeakTimerRef.current)

    if (!acRef.current) acRef.current = new AudioContext()
    if (acRef.current.state === 'suspended') {
      try { await acRef.current.resume() } catch {
        cbRef.current.log(`audio ctx suspended (click first)`, 'err')
        chunkDone(idx)
        return
      }
    }
    if (acRef.current.state !== 'running') {
      cbRef.current.log(`audio ctx not running: ${acRef.current.state}`, 'err')
      chunkDone(idx)
      return
    }

    let buf: AudioBuffer
    try {
      if (ch.decodedAudio) {
        buf = ch.decodedAudio
      } else if (ch.rawBuffer) {
        // Intro path: WAV fetched via HTTP as ArrayBuffer — no atob conversion needed
        buf = await acRef.current.decodeAudioData(ch.rawBuffer.slice(0))
      } else {
        const bytes = Uint8Array.from(atob(ch.audio), (c) => c.charCodeAt(0))
        buf = await acRef.current.decodeAudioData(bytes.buffer)
      }
    } catch (e) {
      cbRef.current.log(`audio decode err: ${(e as Error).message}`, 'err')
      chunkDone(idx)
      return
    }
    if (playbackSession !== playbackSessionRef.current) return

    if (currentSrcRef.current) {
      currentSrcRef.current.onended = null
      try { currentSrcRef.current.stop() } catch { /* ignore */ }
    }

    const cvs = speakCvsRef.current
    const ctx = cvs?.getContext('2d')
    if (!ctx || !cvs) {
      const src = acRef.current.createBufferSource()
      src.buffer = buf
      currentSrcRef.current = src
      src.connect(acRef.current.destination)
      src.onended = () => chunkDone(idx)
      src.start(acRef.current.currentTime)
      return
    }

    // Kick off bitmap decode for the first window; render loop extends this progressively
    for (let i = 0; i < Math.min(PRELOAD_FRAME_WINDOW, ch.frames.length); i += 1) preloadBitmap(ch, i)
    if (playbackSession !== playbackSessionRef.current) return

    const src = acRef.current.createBufferSource()
    src.buffer = buf
    currentSrcRef.current = src
    const ana = acRef.current.createAnalyser()
    ana.fftSize = 32
    src.connect(ana)
    src.connect(acRef.current.destination)
    const t0 = acRef.current.currentTime + 0.02
    src.start(t0)

    renderActiveRef.current = true

    let chunkDoneCalled = false
    const callChunkDone = () => {
      if (!chunkDoneCalled && playbackSession === playbackSessionRef.current) {
        chunkDoneCalled = true
        chunkEndTsRef.current[idx] = performance.now()
        cbRef.current.onChunkPlaybackEnd?.(idx)
        chunkDone(idx)
      }
    }
    src.onended = callChunkDone

    let last = -1
    let renderStartedAt = 0
    let lastDrawTs = 0
    // Instrumentation — accessible in devtools as window.__avatarPerf
    const perf: { t: number; fi: number; gap?: number; note?: string }[] = []
    ;(window as Record<string, unknown>).__avatarPerf ??= []
    ;((window as Record<string, unknown>).__avatarPerf as unknown[]).push({ chunk: idx, events: perf })
    const loop = () => {
      if (playbackSession !== playbackSessionRef.current) return
      if (!renderActiveRef.current || !acRef.current) return
      const elapsed = Math.max(0, acRef.current.currentTime - t0)
      const frameCount = ch.frames.length
      const knownTotal = ch.frameDone ? frameCount : (ch.expectedFrames ?? 0)
      const effectiveFps = knownTotal > 1
        ? (knownTotal - 1) / Math.max(0.001, buf.duration)
        : Math.max(
            1,
            Math.min(
              FPS / Math.max(1, ch.frameStride || 1),
              frameCount > 1
                ? (frameCount - 1) / Math.max(0.001, elapsed + (ch.cached ? 0.75 : LIVE_FRAME_HEADROOM_S))
                : 1,
            ),
          )
      const fi = Math.floor(elapsed * effectiveFps)
      const displayIndex = frameCount > 0 ? Math.min(fi, frameCount - 1) : -1
      // Trigger bitmap decode for upcoming frames (progressive, never bulk)
      if (displayIndex >= 0) {
        for (let i = displayIndex; i < Math.min(displayIndex + PRELOAD_FRAME_WINDOW, frameCount); i += 1) preloadBitmap(ch, i)
      }
      const bitmap = displayIndex >= 0 ? ch.bitmapCache[displayIndex] : undefined
      const now = performance.now()
      if (bitmap && displayIndex !== last) {
        const gap = lastDrawTs > 0 ? now - lastDrawTs : 0
        if (gap > 60) {
          perf.push({ t: now, fi: displayIndex, gap: Math.round(gap), note: 'STUTTER' })
          console.warn(`[avatar] chunk=${idx} stutter ${Math.round(gap)}ms at frame ${displayIndex}/${frameCount} elapsed=${elapsed.toFixed(2)}s`)
        } else {
          perf.push({ t: now, fi: displayIndex })
        }
        lastDrawTs = now
        showSpeak()
        ctx.drawImage(bitmap, 0, 0, CANVAS_W, CANVAS_H)
        last = displayIndex
        if (renderStartedAt === 0) {
          renderStartedAt = now
          const key = `${ch.turnId ?? ''}:${idx}`
          if (!firstRenderReportedRef.current[key]) {
            firstRenderReportedRef.current[key] = true
            cbRef.current.onFirstFrameRender?.(idx, ch.turnId)
          }
          if (idx > 0) {
            const prevEndTs = chunkEndTsRef.current[idx - 1]
            if (prevEndTs) {
              const transitionGap = Math.round(now - prevEndTs)
              perf.push({ t: now, fi: displayIndex, gap: transitionGap, note: `CHUNK_TRANSITION_FROM_${idx - 1}` })
              if (transitionGap > 80) {
                console.warn(`[avatar] chunk transition gap ${transitionGap}ms (chunk ${idx - 1} → ${idx})`)
              } else {
                console.log(`[avatar] chunk transition gap ${transitionGap}ms (chunk ${idx - 1} → ${idx})`)
              }
            }
          }
        }
      } else if (displayIndex >= 0 && !bitmap && lastDrawTs > 0 && now - lastDrawTs > 60) {
        perf.push({ t: now, fi: displayIndex, gap: Math.round(now - lastDrawTs), note: 'BITMAP_NOT_READY' })
        console.warn(`[avatar] chunk=${idx} bitmap ${displayIndex} not ready, gap=${Math.round(now - lastDrawTs)}ms`)
      }
      if (elapsed < buf.duration + 0.2) requestAnimationFrame(loop)
      else callChunkDone()
    }
    requestAnimationFrame(loop)
  }, [speakCvsRef, showSpeak, chunkDone, preloadBitmap])

  useEffect(() => {
    playChunkRef.current = playChunk
  }, [playChunk])

  useEffect(() => {
    maybePlayNextRef.current = maybePlayNext
  }, [maybePlayNext])

  const onAudioReady = useCallback((idx: number, b64: string, frameStride = 1, turnId?: string, cached = false, expectedFrames?: number) => {
    if (isStaleTurn(turnId)) return
    ensureChunk(idx)
    const ch = chunksRef.current[idx]
    ch.audio = b64
    ch.frameStride = Math.max(1, frameStride)
    ch.cached = cached
    if (turnId) ch.turnId = turnId
    if (expectedFrames != null) ch.expectedFrames = expectedFrames
    // Pre-decode audio eagerly while previous chunk may still be playing,
    // so the decodeAudioData cost is not paid at transition time.
    const playbackSession = playbackSessionRef.current
    if (acRef.current && acRef.current.state === 'running') {
      const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))
      acRef.current.decodeAudioData(bytes.buffer.slice(0)).then((buf) => {
        if (playbackSession !== playbackSessionRef.current || isStaleTurn(turnId)) return
        if (chunksRef.current[idx]) chunksRef.current[idx].decodedAudio = buf
      }).catch(() => { /* will retry in playChunk */ })
    }
    maybePlayNext()
  }, [ensureChunk, isStaleTurn, maybePlayNext])

  // Used for intro audio served via HTTP URL — fetches WAV as ArrayBuffer (no atob overhead)
  // and pre-decodes via AudioContext when available.
  const onAudioReadyUrl = useCallback((idx: number, url: string, frameStride = 1, turnId?: string, cached = false, expectedFrames?: number) => {
    if (isStaleTurn(turnId)) return
    ensureChunk(idx)
    const ch = chunksRef.current[idx]
    ch.frameStride = Math.max(1, frameStride)
    ch.cached = cached
    if (turnId) ch.turnId = turnId
    if (expectedFrames != null) ch.expectedFrames = expectedFrames
    const playbackSession = playbackSessionRef.current
    fetch(url)
      .then((r) => r.arrayBuffer())
      .then((buf) => {
        if (playbackSession !== playbackSessionRef.current || isStaleTurn(turnId)) return
        const target = chunksRef.current[idx]
        if (!target) return
        target.rawBuffer = buf
        target.audio = '__url__'  // sentinel: audio data is in rawBuffer, not base64
        // Pre-decode immediately if AudioContext is ready
        if (acRef.current && acRef.current.state === 'running') {
          acRef.current.decodeAudioData(buf.slice(0)).then((decoded) => {
            if (playbackSession !== playbackSessionRef.current || isStaleTurn(turnId)) return
            if (chunksRef.current[idx]) chunksRef.current[idx].decodedAudio = decoded
            maybePlayNext()
          }).catch(() => { /* decoded in playChunk fallback */ })
        }
        maybePlayNext()
      })
      .catch((e) => {
        console.error('[audio_url fetch failed]', url, e)
        if (chunksRef.current[idx]) {
          chunksRef.current[idx].error = true
          chunksRef.current[idx].frameDone = true
        }
      })
  }, [ensureChunk, isStaleTurn, maybePlayNext])

  const onFrame = useCallback((idx: number, b64: string, turnId?: string) => {
    if (isStaleTurn(turnId)) return
    ensureChunk(idx)
    if (turnId) chunksRef.current[idx].turnId = turnId
    const ch = chunksRef.current[idx]
    const frameIdx = ch.frames.length
    ch.frames.push(b64)
    preloadBitmap(ch, frameIdx)
    if (idx === nextPlayChunkRef.current) maybePlayNext()
  }, [ensureChunk, isStaleTurn, maybePlayNext, preloadBitmap])

  const onFrameCache = useCallback((idx: number, url: string, turnId?: string) => {
    if (isStaleTurn(turnId)) return
    const playbackSession = playbackSessionRef.current
    ensureChunk(idx)
    const ch = chunksRef.current[idx]
    ch.cached = true
    ch.frameCacheLoading = true
    if (turnId) ch.turnId = turnId
    const fetchRange = async (start: number, limit: number) => {
      const joiner = url.includes('?') ? '&' : '?'
      const controller = new AbortController()
      frameCacheControllersRef.current.add(controller)
      const timer = window.setTimeout(() => controller.abort(), FRAME_CACHE_FETCH_TIMEOUT_MS)
      try {
        const response = await fetch(`${url}${joiner}start=${start}&limit=${limit}`, {
          cache: 'force-cache',
          signal: controller.signal,
        })
        if (!response.ok) throw new Error(`frame cache fetch failed: ${response.status}`)
        return response.json() as Promise<{ frames?: unknown[]; end?: number; total?: number; has_more?: boolean }>
      } finally {
        window.clearTimeout(timer)
        frameCacheControllersRef.current.delete(controller)
      }
    }
    const load = async () => {
      try {
        // First small batch so playback can start immediately
        const first = await fetchRange(0, FRAME_CACHE_INITIAL_LIMIT)
        if (playbackSession !== playbackSessionRef.current || isStaleTurn(turnId)) return
        const firstFrames = Array.isArray(first.frames)
          ? first.frames.map((frame) => String(frame)).filter(Boolean)
          : []
        if (firstFrames.length) {
          const offset = ch.frames.length
          ch.frames.push(...firstFrames)
          // Pre-decode only the initial batch — render loop handles the rest progressively
          for (let i = 0; i < firstFrames.length; i++) preloadBitmap(ch, offset + i)
        }
        ch.frameCacheLoading = false
        if (idx === nextPlayChunkRef.current) maybePlayNext()

        if (first.has_more) {
          // Yield one macrotask so playback can begin, then load all remaining in one request
          await new Promise((resolve) => window.setTimeout(resolve, 0))
          if (playbackSession !== playbackSessionRef.current || isStaleTurn(turnId)) return
          const nextStart = Number(first.end ?? firstFrames.length)
          const remaining = Number(first.total ?? 0) - nextStart
          if (remaining > 0) {
            const rest = await fetchRange(nextStart, remaining)
            if (playbackSession !== playbackSessionRef.current || isStaleTurn(turnId)) return
            const restFrames = Array.isArray(rest.frames)
              ? rest.frames.map((frame) => String(frame)).filter(Boolean)
              : []
            // Just push frames — render loop preloads bitmaps progressively (PRELOAD_FRAME_WINDOW ahead)
            if (restFrames.length) ch.frames.push(...restFrames)
          }
        }
        ch.frameDone = true
        if (idx === nextPlayChunkRef.current) maybePlayNext()
      } catch (error) {
        if (playbackSession !== playbackSessionRef.current) return
        ch.error = true
        ch.frameDone = true
        ch.frameCacheLoading = false
        cbRef.current.log(error instanceof Error ? error.message : 'frame cache fetch failed', 'err')
        if (idx === nextPlayChunkRef.current && !isPlayingRef.current) chunkDone(idx)
        else if (idx === nextPlayChunkRef.current) maybePlayNext()
      }
    }
    void load()
  }, [chunkDone, ensureChunk, isStaleTurn, maybePlayNext, preloadBitmap])

  const onChunkDone = useCallback((idx: number, turnId?: string) => {
    if (isStaleTurn(turnId)) return
    ensureChunk(idx)
    if (turnId) chunksRef.current[idx].turnId = turnId
    chunksRef.current[idx].frameDone = true
    maybePlayNext()
  }, [ensureChunk, isStaleTurn, maybePlayNext])

  const onChunkError = useCallback((idx: number, turnId?: string) => {
    if (isStaleTurn(turnId)) return
    ensureChunk(idx)
    if (turnId) chunksRef.current[idx].turnId = turnId
    chunksRef.current[idx].error = true
    chunksRef.current[idx].frameDone = true
    if (idx === nextPlayChunkRef.current && !isPlayingRef.current) chunkDone(idx)
  }, [chunkDone, ensureChunk, isStaleTurn])

  const onAllDone = useCallback((n: number) => {
    totalChunksRef.current = n
    if (!isPlayingRef.current && nextPlayChunkRef.current >= totalChunksRef.current) {
      cbRef.current.onAllChunksDone()
      if (hideSpeakTimerRef.current) window.clearTimeout(hideSpeakTimerRef.current)
      scheduleHideSpeak(140)
    }
  }, [scheduleHideSpeak])

  const startStream = useCallback((turnId?: string) => {
    playbackSessionRef.current += 1
    streamActiveRef.current = true
    isPlayingRef.current = false
    renderActiveRef.current = false
    if (currentSrcRef.current) {
      currentSrcRef.current.onended = null
      try { currentSrcRef.current.stop() } catch { /* ignore */ }
      currentSrcRef.current = null
    }
    for (const controller of frameCacheControllersRef.current) controller.abort()
    frameCacheControllersRef.current.clear()
    if (chunkGapTimerRef.current) window.clearTimeout(chunkGapTimerRef.current)
    chunksRef.current = {}
    nextPlayChunkRef.current = 0
    totalChunksRef.current = Infinity
    firstRenderReportedRef.current = {}
    activeTurnIdRef.current = turnId ?? null
  }, [])

  const setStreamActive = useCallback((active: boolean) => {
    streamActiveRef.current = active
  }, [])

  return {
    ensureAudioContext,
    stopPlayback,
    onAudioReady,
    onAudioReadyUrl,
    onFrameCache,
    onFrame,
    onChunkDone,
    onChunkError,
    onAllDone,
    startStream,
    setStreamActive,
    isPlayingRef,
    streamActiveRef,
    maybePlayNext,
  }
}
