/* eslint-disable react-hooks/immutability */
import { useRef, useCallback, useEffect } from 'react'
import type { ChunkState } from '../types'
import { FPS, CANVAS_W, CANVAS_H, CHUNK_GAP_HOLD_MS } from '../constants'

const LIVE_FRAME_HEADROOM_S = 0.14

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
    renderActiveRef.current = false
    isPlayingRef.current = false
    streamActiveRef.current = false
    if (hideSpeakTimerRef.current) window.clearTimeout(hideSpeakTimerRef.current)
    if (chunkGapTimerRef.current) window.clearTimeout(chunkGapTimerRef.current)
    if (currentSrcRef.current) {
      try { currentSrcRef.current.stop() } catch { /* ignore */ }
      currentSrcRef.current = null
    }
    chunksRef.current = {}
    nextPlayChunkRef.current = 0
    totalChunksRef.current = Infinity
    firstRenderReportedRef.current = {}
    hideSpeak()
  }, [hideSpeak])

  const ensureChunk = useCallback((idx: number) => {
    if (!chunksRef.current[idx]) chunksRef.current[idx] = { audio: null, frames: [], frameDone: false, frameStride: 1 }
  }, [])

  const isChunkReadyToPlay = useCallback((_idx: number, ch: ChunkState | undefined) => {
    if (!ch?.audio) return false
    return true
  }, [])

  const chunkDone = useCallback((idx: number) => {
    renderActiveRef.current = false
    isPlayingRef.current = false
    nextPlayChunkRef.current = idx + 1

    const nextChunk = chunksRef.current[nextPlayChunkRef.current]
    if (isChunkReadyToPlay(nextPlayChunkRef.current, nextChunk)) {
      maybePlayNext()
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
    chunkGapTimerRef.current = window.setTimeout(() => {
      if (!isPlayingRef.current && streamActiveRef.current) scheduleHideSpeak(180)
    }, CHUNK_GAP_HOLD_MS)
  }, [scheduleHideSpeak])

  const maybePlayNext = useCallback(() => {
    if (isPlayingRef.current) return
    const ch = chunksRef.current[nextPlayChunkRef.current]
    if (isChunkReadyToPlay(nextPlayChunkRef.current, ch)) {
      if (chunkGapTimerRef.current) window.clearTimeout(chunkGapTimerRef.current)
      void playChunk(nextPlayChunkRef.current)
    }
  }, [isChunkReadyToPlay])

  const playChunk = useCallback(async (idx: number) => {
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

    const bytes = Uint8Array.from(atob(ch.audio), (c) => c.charCodeAt(0))
    let buf: AudioBuffer
    try {
      buf = await acRef.current.decodeAudioData(bytes.buffer)
    } catch (e) {
      cbRef.current.log(`audio decode err: ${(e as Error).message}`, 'err')
      chunkDone(idx)
      return
    }

    if (currentSrcRef.current) {
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

    const cache: Record<number, HTMLImageElement> = {}
    const getImg = (i: number) => {
      if (!cache[i] && ch.frames[i]) {
        const img = new Image()
        img.src = `data:image/jpeg;base64,${ch.frames[i]}`
        cache[i] = img
      }
      return cache[i]
    }

    for (let i = 0; i < Math.min(8, ch.frames.length); i += 1) getImg(i)
    const firstImg = getImg(0)
    if (firstImg && !firstImg.complete) {
      try { await firstImg.decode() } catch { /* continue; the render loop will retry */ }
    }

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
      if (!chunkDoneCalled) {
        chunkDoneCalled = true
        cbRef.current.onChunkPlaybackEnd?.(idx)
        chunkDone(idx)
      }
    }
    src.onended = callChunkDone

    let last = -1
    let renderStartedAt = 0
    const loop = () => {
      if (!renderActiveRef.current || !acRef.current) return
      const elapsed = Math.max(0, acRef.current.currentTime - t0)
      const frameCount = ch.frames.length
      const effectiveFps = ch.frameDone && frameCount > 1
        ? (frameCount - 1) / Math.max(0.001, buf.duration)
        : Math.max(
            1,
            Math.min(
              FPS / Math.max(1, ch.frameStride || 1),
              frameCount > 1 ? (frameCount - 1) / Math.max(0.001, elapsed + LIVE_FRAME_HEADROOM_S) : 1,
            ),
          )
      const fi = Math.floor(elapsed * effectiveFps)
      const displayIndex = frameCount > 0 ? Math.min(fi, frameCount - 1) : -1
      if (displayIndex >= 0) {
        for (let i = displayIndex; i < Math.min(displayIndex + 8, frameCount); i += 1) getImg(i)
      }
      const img = displayIndex >= 0 ? getImg(displayIndex) : undefined
      if (img?.complete && displayIndex !== last) {
        showSpeak()
        ctx.drawImage(img, 0, 0, CANVAS_W, CANVAS_H)
        last = displayIndex
        if (idx === 0 && renderStartedAt === 0) {
          renderStartedAt = performance.now()
          const key = `${ch.turnId ?? ''}:${idx}`
          if (!firstRenderReportedRef.current[key]) {
            firstRenderReportedRef.current[key] = true
            cbRef.current.onFirstFrameRender?.(idx, ch.turnId)
          }
        }
      }
      if (elapsed < buf.duration + 0.2) requestAnimationFrame(loop)
      else callChunkDone()
    }
    requestAnimationFrame(loop)
  }, [speakCvsRef, showSpeak, chunkDone])

  const onAudioReady = useCallback((idx: number, b64: string, frameStride = 1, turnId?: string) => {
    ensureChunk(idx)
    chunksRef.current[idx].audio = b64
    chunksRef.current[idx].frameStride = Math.max(1, frameStride)
    if (turnId) chunksRef.current[idx].turnId = turnId
    maybePlayNext()
  }, [ensureChunk, maybePlayNext])

  const onFrame = useCallback((idx: number, b64: string, turnId?: string) => {
    ensureChunk(idx)
    if (turnId) chunksRef.current[idx].turnId = turnId
    chunksRef.current[idx].frames.push(b64)
    if (idx === nextPlayChunkRef.current) maybePlayNext()
  }, [ensureChunk, maybePlayNext])

  const onChunkDone = useCallback((idx: number, turnId?: string) => {
    ensureChunk(idx)
    if (turnId) chunksRef.current[idx].turnId = turnId
    chunksRef.current[idx].frameDone = true
    maybePlayNext()
  }, [ensureChunk, maybePlayNext])

  const onAllDone = useCallback((n: number) => {
    totalChunksRef.current = n
    if (!isPlayingRef.current && nextPlayChunkRef.current >= totalChunksRef.current) {
      cbRef.current.onAllChunksDone()
      if (hideSpeakTimerRef.current) window.clearTimeout(hideSpeakTimerRef.current)
      scheduleHideSpeak(140)
    }
  }, [scheduleHideSpeak])

  const startStream = useCallback(() => {
    streamActiveRef.current = true
    isPlayingRef.current = false
    renderActiveRef.current = false
    if (chunkGapTimerRef.current) window.clearTimeout(chunkGapTimerRef.current)
    chunksRef.current = {}
    nextPlayChunkRef.current = 0
    totalChunksRef.current = Infinity
    firstRenderReportedRef.current = {}
  }, [])

  const setStreamActive = useCallback((active: boolean) => {
    streamActiveRef.current = active
  }, [])

  return {
    ensureAudioContext,
    stopPlayback,
    onAudioReady,
    onFrame,
    onChunkDone,
    onAllDone,
    startStream,
    setStreamActive,
    isPlayingRef,
    streamActiveRef,
    maybePlayNext,
  }
}
