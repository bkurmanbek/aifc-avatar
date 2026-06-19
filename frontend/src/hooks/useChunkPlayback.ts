import { useRef, useCallback, useEffect } from 'react'
import type { ChunkState } from '../types'
import { FPS, CANVAS_W, CANVAS_H } from '../constants'
import { backendHttpUrl } from '../utils'

const LIVE_FRAME_HEADROOM_S = 0.14
const LIVE_READY_FRAME_HEADROOM = 10
const CACHED_READY_FRAME_HEADROOM = 32
const PRELOAD_FRAME_WINDOW = 48
// Frames of the NEXT chunk to decode during the tail of the current chunk, so the boundary
// handoff finds them GPU-ready. Narrow so it doesn't steal decode bandwidth from the tail.
const CROSS_CHUNK_PRELOAD = 12
// Build a startup lead (seconds of buffered audio) before the first live chunk plays.
// TTS streams at ~realtime, so playback consumes audio as fast as it's produced; without
// a lead, playback races production and starves whenever SyncTalk render jitters, causing
// recurring transition gaps. The lead must exceed (max segment duration + render margin)
// so the next segment is always ready before the current one ends.
// 2.0s lead: 1.4s caused transition stutters in practice (production couldn't stay ahead on
// jittery networks); 2.0 restores margin while still ~0.2s faster perceived start than the
// original 2.2. Raise toward 2.2+ if stutters persist; lower only if the network is reliably fast.
const LIVE_PREBUFFER_S = 2.0
// How many chunks ahead of the playing one we pre-schedule audio for (gapless seams).
const AUDIO_SCHEDULE_LOOKAHEAD = 6
const FRAME_CACHE_INITIAL_LIMIT = 48
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
  // Gapless audio scheduling state
  const audioCursorRef = useRef(0)            // next AudioContext start time
  const scheduledSourcesRef = useRef<Set<AudioBufferSourceNode>>(new Set())
  const highestScheduledRef = useRef(-1)      // highest chunk idx whose audio is scheduled
  const playbackStartedRef = useRef(false)    // first live chunk has begun (prebuffer satisfied)
  const prescheduleAheadRef = useRef<() => void>(() => {})
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

  // Tracks whether the canvas is currently shown so the per-frame render loop can
  // call showSpeak() without re-writing inline styles every frame. Writing
  // c.style.opacity + classList each of the 25 frames/sec forces the browser to
  // re-evaluate the `:has(#speakCvs.show)` selector and recalc styles on the main
  // thread every frame, stealing time from the render loop. Only touch the DOM on
  // the actual hidden→shown transition.
  const speakShownRef = useRef(false)

  const showSpeak = useCallback(() => {
    if (speakShownRef.current) return
    const c = speakCvsRef.current
    if (!c) return
    c.classList.add('show')
    c.style.opacity = '1'
    speakShownRef.current = true
  }, [speakCvsRef])

  const hideSpeak = useCallback(() => {
    const c = speakCvsRef.current
    if (!c) return
    speakShownRef.current = false
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

  // Stop every audio source scheduled on the timeline (gapless playback queues sources
  // ahead of the playhead, so an interrupt must cancel all of them) and reset the cursor.
  const stopAllScheduledAudio = useCallback(() => {
    for (const src of scheduledSourcesRef.current) {
      src.onended = null
      try { src.stop() } catch { /* ignore */ }
    }
    scheduledSourcesRef.current.clear()
    audioCursorRef.current = 0
    highestScheduledRef.current = -1
    playbackStartedRef.current = false
  }, [])

  // Close every decoded ImageBitmap across all chunks so GPU memory is released
  // immediately on reset rather than waiting for GC to finalize the bitmaps.
  const releaseAllBitmaps = useCallback(() => {
    for (const key of Object.keys(chunksRef.current)) {
      const ch = chunksRef.current[Number(key)]
      if (!ch) continue
      for (const fi of Object.keys(ch.bitmapCache)) ch.bitmapCache[Number(fi)]?.close?.()
      ch.bitmapCache = {}
    }
  }, [])

  const stopPlayback = useCallback(() => {
    playbackSessionRef.current += 1
    renderActiveRef.current = false
    isPlayingRef.current = false
    streamActiveRef.current = false
    // Reset perf log on new session so stale data doesn't accumulate
    ;(window as unknown as Record<string, unknown>).__avatarPerf = []
    if (hideSpeakTimerRef.current) window.clearTimeout(hideSpeakTimerRef.current)
    if (chunkGapTimerRef.current) window.clearTimeout(chunkGapTimerRef.current)
    if (currentSrcRef.current) {
      currentSrcRef.current.onended = null
      try { currentSrcRef.current.stop() } catch { /* ignore */ }
      currentSrcRef.current = null
    }
    stopAllScheduledAudio()
    for (const controller of frameCacheControllersRef.current) controller.abort()
    frameCacheControllersRef.current.clear()
    releaseAllBitmaps()
    chunksRef.current = {}
    nextPlayChunkRef.current = 0
    totalChunksRef.current = Infinity
    firstRenderReportedRef.current = {}
    chunkEndTsRef.current = {}
    activeTurnIdRef.current = null
    hideSpeak()
  }, [hideSpeak, releaseAllBitmaps, stopAllScheduledAudio])

  const isStaleTurn = useCallback((turnId?: string) => {
    return Boolean(turnId && activeTurnIdRef.current !== turnId)
  }, [])

  const ensureChunk = useCallback((idx: number) => {
    if (!chunksRef.current[idx]) chunksRef.current[idx] = { audio: null, frames: [], bitmapCache: {}, bitmapPending: new Set(), frameDone: false, frameStride: 1 }
  }, [])

  // Decode one JPEG frame off-thread via createImageBitmap and cache the GPU bitmap.
  // We decode the base64 into a Blob and pass that to createImageBitmap rather than
  // an HTMLImageElement: createImageBitmap(img) rejects with InvalidStateError when the
  // image hasn't finished loading (which it never has, synchronously after setting src),
  // so the Image path silently failed to populate the cache and every frame stuttered.
  // createImageBitmap(blob) decodes fully off the main thread with no load race.
  const preloadBitmap = useCallback((ch: ChunkState, frameIdx: number) => {
    const frame = ch.frames[frameIdx]
    if (ch.bitmapCache[frameIdx] || ch.bitmapPending.has(frameIdx) || !frame) return
    ch.bitmapPending.add(frameIdx)
    let blob: Blob
    if (frame instanceof Blob) {
      // Binary WS frame — already raw JPEG bytes, no base64 decode needed.
      blob = frame
    } else {
      try {
        const binary = atob(frame)
        const bytes = new Uint8Array(binary.length)
        for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i)
        blob = new Blob([bytes], { type: 'image/jpeg' })
      } catch {
        ch.bitmapPending.delete(frameIdx)
        return
      }
    }
    createImageBitmap(blob).then(bitmap => {
      ch.bitmapPending.delete(frameIdx)
      // Close-before-overwrite: ensureBitmapReady may have already decoded this frame.
      // Overwriting without closing orphans a GPU-backed bitmap until GC (a leak).
      if (ch.bitmapCache[frameIdx]) bitmap.close?.()
      else ch.bitmapCache[frameIdx] = bitmap
    }).catch(() => {
      ch.bitmapPending.delete(frameIdx)
    })
  }, [])

  // Decode a frame's bitmap and await it. Used to guarantee the first frame(s) of a
  // chunk are GPU-ready before its render loop starts, so playback doesn't open with a
  // stutter while createImageBitmap is still in flight.
  const ensureBitmapReady = useCallback(async (ch: ChunkState, frameIdx: number) => {
    if (ch.bitmapCache[frameIdx]) return
    // If preloadBitmap is already decoding this frame, don't decode a second copy.
    if (ch.bitmapPending.has(frameIdx)) return
    const frame = ch.frames[frameIdx]
    if (!frame) return
    // Register so a concurrent preloadBitmap skips this frame (its guard checks pending).
    ch.bitmapPending.add(frameIdx)
    try {
      let blob: Blob
      if (frame instanceof Blob) {
        blob = frame
      } else {
        const binary = atob(frame)
        const bytes = new Uint8Array(binary.length)
        for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i)
        blob = new Blob([bytes], { type: 'image/jpeg' })
      }
      const bitmap = await createImageBitmap(blob)
      ch.bitmapPending.delete(frameIdx)
      if (ch.bitmapCache[frameIdx]) bitmap.close?.()
      else ch.bitmapCache[frameIdx] = bitmap
    } catch { /* render loop will retry via preloadBitmap */ }
  }, [])

  const isChunkReadyToPlay = useCallback((_idx: number, ch: ChunkState | undefined) => {
    if (!ch?.audio) return false
    if (ch.error) return false
    if (ch.cached) return ch.frames.length >= CACHED_READY_FRAME_HEADROOM || (ch.frameDone && ch.frames.length > 0)
    if (ch.frames.length >= LIVE_READY_FRAME_HEADROOM) return true
    if (ch.frameDone && ch.frames.length > 0) return true
    return false
  }, [])

  // Decode a chunk's WAV to an AudioBuffer off the critical path (idempotent).
  const ensureDecoded = useCallback((ch: ChunkState) => {
    if (ch.decodedAudio || ch.decoding) return
    const ac = acRef.current
    if (!ac || ac.state !== 'running') return
    const session = playbackSessionRef.current
    const finish = (buf: AudioBuffer | null) => {
      ch.decoding = false
      if (session !== playbackSessionRef.current) return
      if (buf) {
        ch.decodedAudio = buf
        prescheduleAheadRef.current()
      }
    }
    try {
      if (ch.rawBuffer) {
        ch.decoding = true
        ac.decodeAudioData(ch.rawBuffer.slice(0)).then(finish).catch(() => finish(null))
      } else if (ch.audio && ch.audio !== '__url__') {
        ch.decoding = true
        const bytes = Uint8Array.from(atob(ch.audio), (c) => c.charCodeAt(0))
        ac.decodeAudioData(bytes.buffer).then(finish).catch(() => finish(null))
      }
    } catch {
      ch.decoding = false
    }
  }, [])

  // Queue a chunk's audio onto the AudioContext timeline immediately after the previous
  // chunk's audio (audioCursorRef). Starting sources ahead of the playhead is what makes
  // mid-sentence segment seams gapless. Returns true once the chunk's audio is scheduled.
  const scheduleChunkAudio = useCallback((idx: number): boolean => {
    const ch = chunksRef.current[idx]
    const ac = acRef.current
    if (!ch || !ac || ac.state !== 'running') return false
    if (ch.scheduledSource) return true
    if (!ch.decodedAudio) { ensureDecoded(ch); return false }
    const buf = ch.decodedAudio
    const cursor = audioCursorRef.current
    const t0 = cursor > ac.currentTime + 0.02 ? cursor : ac.currentTime + 0.08
    const src = ac.createBufferSource()
    src.buffer = buf
    src.connect(ac.destination)
    try { src.start(t0) } catch { return false }
    ch.scheduledSource = src
    ch.scheduledT0 = t0
    ch.scheduledDuration = buf.duration
    audioCursorRef.current = t0 + buf.duration
    highestScheduledRef.current = Math.max(highestScheduledRef.current, idx)
    scheduledSourcesRef.current.add(src)
    src.onended = () => { scheduledSourcesRef.current.delete(src) }
    return true
  }, [ensureDecoded])

  // Fill the audio timeline ahead of the playhead with upcoming contiguous ready chunks.
  const prescheduleAhead = useCallback(() => {
    if (!playbackStartedRef.current) return
    let idx = highestScheduledRef.current + 1
    const limit = highestScheduledRef.current + AUDIO_SCHEDULE_LOOKAHEAD
    while (idx <= limit && idx < totalChunksRef.current) {
      const ch = chunksRef.current[idx]
      if (!isChunkReadyToPlay(idx, ch)) {
        if (ch) ensureDecoded(ch)
        break
      }
      if (!scheduleChunkAudio(idx)) break
      idx += 1
    }
  }, [isChunkReadyToPlay, scheduleChunkAudio, ensureDecoded])

  useEffect(() => { prescheduleAheadRef.current = prescheduleAhead }, [prescheduleAhead])

  // Startup gate: only begin the first live chunk once enough audio is buffered that the
  // next segment will always be ready before the current one ends (prevents gaps).
  const prebufferReady = useCallback(() => {
    const first = chunksRef.current[0]
    if (!first) return false
    if (first.cached) return true            // cached/intro path manages its own headroom
    // Sum the audio duration of contiguous ready chunks from 0. NOTE: we deliberately do
    // NOT short-circuit on chunk 0 being frameDone — chunk 0 is a ~1s segment that finishes
    // rendering almost immediately, and starting then would leave a near-zero lead (the
    // root cause of recurring chunk-transition gaps). We require a real time lead instead.
    let seconds = 0
    let i = 0
    for (; ; i += 1) {
      const ch = chunksRef.current[i]
      if (!isChunkReadyToPlay(i, ch)) break
      const frameCount = ch.frameDone ? ch.frames.length : (ch.expectedFrames ?? ch.frames.length)
      seconds += frameCount / FPS
    }
    // Whole turn already delivered and contiguous-ready → start now (short answers).
    if (totalChunksRef.current !== Infinity && i >= totalChunksRef.current) return true
    return seconds >= LIVE_PREBUFFER_S
  }, [isChunkReadyToPlay])

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
    const idx = nextPlayChunkRef.current
    const ch = chunksRef.current[idx]
    if (!isChunkReadyToPlay(idx, ch)) return
    // First live chunk waits for the startup prebuffer; later chunks play as soon as ready.
    if (!playbackStartedRef.current && idx === 0 && !ch?.cached && !prebufferReady()) return
    if (chunkGapTimerRef.current) window.clearTimeout(chunkGapTimerRef.current)
    playChunkRef.current(idx)
  }, [isChunkReadyToPlay, prebufferReady])

  const playChunk = useCallback(async (idx: number) => {
    const playbackSession = playbackSessionRef.current
    const ch = chunksRef.current[idx]
    if (!ch?.audio) return
    // Claim playback synchronously to prevent a concurrent maybePlayNext (e.g. from an
    // incoming frame) from re-entering playChunk for the same idx during the awaits below.
    isPlayingRef.current = true

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

    // Ensure this chunk's audio is decoded (it usually was pre-decoded on arrival).
    if (!ch.decodedAudio) {
      try {
        if (ch.rawBuffer) {
          ch.decodedAudio = await acRef.current.decodeAudioData(ch.rawBuffer.slice(0))
        } else if (ch.audio !== '__url__') {
          const bytes = Uint8Array.from(atob(ch.audio), (c) => c.charCodeAt(0))
          ch.decodedAudio = await acRef.current.decodeAudioData(bytes.buffer)
        }
      } catch (e) {
        cbRef.current.log(`audio decode err: ${(e as Error).message}`, 'err')
        chunkDone(idx)
        return
      }
    }
    if (playbackSession !== playbackSessionRef.current) return
    const buf = ch.decodedAudio
    if (!buf) { chunkDone(idx); return }

    playbackStartedRef.current = true
    renderActiveRef.current = true
    cbRef.current.onChunkPlaybackStart?.(idx)
    cbRef.current.setMode('speaking')
    if (hideSpeakTimerRef.current) window.clearTimeout(hideSpeakTimerRef.current)

    const cvs = speakCvsRef.current
    const ctx = cvs?.getContext('2d')
    const wasPreScheduled = !!ch.scheduledSource

    // Kick off bitmap decode for the opening window; render loop extends this progressively.
    if (ctx && cvs) {
      for (let i = 0; i < Math.min(PRELOAD_FRAME_WINDOW, ch.frames.length); i += 1) preloadBitmap(ch, i)
    }

    // Cold start (chunk 0 / not pre-scheduled): decode the opening frames BEFORE scheduling
    // audio, so audio t0 aligns with frame 0 being ready. If we scheduled audio first and then
    // awaited, the audio would advance during the cold decode and the render loop would skip
    // past the very frames we just decoded → an opening stutter. Gapless chunks are already
    // scheduled and their opening bitmaps were preloaded during the previous chunk, so we must
    // NOT block here — blocking a fixed-timeline chunk only desyncs the handoff.
    if (ctx && cvs && !wasPreScheduled) {
      await Promise.all([ensureBitmapReady(ch, 0), ensureBitmapReady(ch, 1), ensureBitmapReady(ch, 2)])
      if (playbackSession !== playbackSessionRef.current) return
    }

    if (!ch.scheduledSource) scheduleChunkAudio(idx)
    const t0 = ch.scheduledT0 ?? (acRef.current.currentTime + 0.05)
    const chunkDuration = ch.scheduledDuration ?? buf.duration
    prescheduleAhead()

    if (!ctx || !cvs) {
      // No canvas: audio is already scheduled; end the chunk when its audio finishes.
      const ms = Math.max(0, (t0 + chunkDuration - acRef.current.currentTime) * 1000)
      window.setTimeout(() => { if (playbackSession === playbackSessionRef.current) chunkDone(idx) }, ms)
      return
    }

    let chunkDoneCalled = false
    const callChunkDone = () => {
      if (!chunkDoneCalled && playbackSession === playbackSessionRef.current) {
        chunkDoneCalled = true
        chunkEndTsRef.current[idx] = performance.now()
        cbRef.current.onChunkPlaybackEnd?.(idx)
        chunkDone(idx)
      }
    }

    let last = -1
    let lastEvicted = -1
    let renderStartedAt = 0
    let lastDrawTs = 0
    // Playback is strictly forward-only, so once we've drawn past a frame we never
    // need its bitmap again. Free it promptly: keeping every decoded ImageBitmap for
    // the whole chunk (e.g. 548 intro frames × several MB each) builds up hundreds of
    // MB of GPU memory and the resulting GC/allocation churn causes periodic ~60-80ms
    // hitches. Keep a tiny trailing margin behind the playhead.
    const EVICT_BEHIND = 4
    const evictPlayed = (upToExclusive: number) => {
      for (let i = lastEvicted + 1; i < upToExclusive; i += 1) {
        const b = ch.bitmapCache[i]
        if (b) {
          b.close?.()
          delete ch.bitmapCache[i]
        }
      }
      if (upToExclusive - 1 > lastEvicted) lastEvicted = upToExclusive - 1
    }
    // Instrumentation — accessible in devtools as window.__avatarPerf
    const perf: { t: number; fi: number; gap?: number; note?: string }[] = []
    ;(window as unknown as Record<string, unknown>).__avatarPerf ??= []
    ;((window as unknown as Record<string, unknown>).__avatarPerf as unknown[]).push({ chunk: idx, events: perf })
    const loop = () => {
      if (playbackSession !== playbackSessionRef.current) return
      if (!renderActiveRef.current || !acRef.current) return
      const elapsed = Math.max(0, acRef.current.currentTime - t0)
      const frameCount = ch.frames.length
      const knownTotal = ch.frameDone ? frameCount : (ch.expectedFrames ?? 0)
      const effectiveFps = knownTotal > 1
        ? (knownTotal - 1) / Math.max(0.001, chunkDuration)
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
        // Cross-chunk lookahead: in the last ~0.5s of this chunk, decode the NEXT chunk's
        // opening frames so the boundary handoff finds them already GPU-ready (the per-chunk
        // window above never spans into the next chunk, which caused the start-of-chunk
        // stutter). Kept narrow so it doesn't steal decode bandwidth from this chunk's tail.
        const remaining = frameCount - 1 - displayIndex
        if (remaining < CROSS_CHUNK_PRELOAD) {
          const nextCh = chunksRef.current[idx + 1]
          if (nextCh) {
            for (let i = 0; i < Math.min(CROSS_CHUNK_PRELOAD, nextCh.frames.length); i += 1) preloadBitmap(nextCh, i)
          }
        }
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
        evictPlayed(displayIndex - EVICT_BEHIND)
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
      // Hand off exactly at the audio boundary — the next chunk's audio is already
      // scheduled at t0+chunkDuration, so starting its render loop now keeps both
      // audio and frames gapless.
      if (elapsed < chunkDuration) requestAnimationFrame(loop)
      else callChunkDone()
    }
    requestAnimationFrame(loop)
  }, [speakCvsRef, showSpeak, chunkDone, preloadBitmap, ensureBitmapReady, scheduleChunkAudio, prescheduleAhead])

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
        prescheduleAheadRef.current()  // queue this chunk's audio onto the timeline if it's next
      }).catch(() => { /* will retry in playChunk */ })
    }
    maybePlayNext()
    prescheduleAheadRef.current()
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
    fetch(backendHttpUrl(url), { headers: { 'ngrok-skip-browser-warning': 'true' } })
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
    prescheduleAheadRef.current()  // upcoming chunk may now have enough frames to schedule
  }, [ensureChunk, isStaleTurn, maybePlayNext, preloadBitmap])

  // Binary WS frame — store the raw JPEG bytes as a Blob; preloadBitmap decodes it
  // directly via createImageBitmap with no base64/JSON cost on the main thread.
  const onFrameBinary = useCallback((idx: number, turnId: string | undefined, jpeg: ArrayBuffer) => {
    if (isStaleTurn(turnId)) return
    ensureChunk(idx)
    if (turnId) chunksRef.current[idx].turnId = turnId
    const ch = chunksRef.current[idx]
    const frameIdx = ch.frames.length
    ch.frames.push(new Blob([jpeg], { type: 'image/jpeg' }))
    preloadBitmap(ch, frameIdx)
    if (idx === nextPlayChunkRef.current) maybePlayNext()
    prescheduleAheadRef.current()  // upcoming chunk may now have enough frames to schedule
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
        const response = await fetch(`${backendHttpUrl(url)}${joiner}start=${start}&limit=${limit}`, {
          cache: 'force-cache',
          signal: controller.signal,
          headers: { 'ngrok-skip-browser-warning': 'true' },
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
    prescheduleAheadRef.current()
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
    stopAllScheduledAudio()
    for (const controller of frameCacheControllersRef.current) controller.abort()
    frameCacheControllersRef.current.clear()
    if (chunkGapTimerRef.current) window.clearTimeout(chunkGapTimerRef.current)
    releaseAllBitmaps()
    chunksRef.current = {}
    nextPlayChunkRef.current = 0
    totalChunksRef.current = Infinity
    firstRenderReportedRef.current = {}
    activeTurnIdRef.current = turnId ?? null
  }, [releaseAllBitmaps, stopAllScheduledAudio])

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
    onFrameBinary,
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
