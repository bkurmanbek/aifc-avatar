import { useRef, useCallback, useEffect } from 'react'
import { VAD_SILENCE_LEVEL, AUTO_ENDPOINT_MS, MIN_RECORD_MS, VAD_INTERVAL_MS, PARTIAL_SEND_INTERVAL } from '../constants'
import { encodeBase64 } from '../utils'

export interface MicCallbacks {
  setMode: (mode: string) => void
  setIsListening: (listening: boolean) => void
  setLog: (text: string, cls?: string) => void
  setBusy: (busy: boolean) => void
  setPartial: (text: string) => void
  sendWs: (payload: unknown) => void
  ensureAudioContext: () => void
  resetIdleTimer: () => void
}

export function useMicrophone(
  callbacks: MicCallbacks,
  vadHolderRef: React.RefObject<HTMLDivElement | null>,
) {
  const cbRef = useRef(callbacks)

  useEffect(() => {
    cbRef.current = callbacks
  }, [callbacks])

  const mediaRecorderRef = useRef<MediaRecorder | null>(null)
  const audioCtxNodeRef = useRef<AudioContext | null>(null)
  const analyserRef = useRef<AnalyserNode | null>(null)
  const micStreamRef = useRef<MediaStream | null>(null)
  const isListeningRef = useRef(false)
  const vadTimerRef = useRef<number | null>(null)
  const silenceMsRef = useRef(0)
  const listenStartedAtRef = useRef(0)
  const audioChunksRef = useRef<Blob[]>([])
  const partialChunksRef = useRef<Blob[]>([])
  const partialSendCountRef = useRef(0)

  const updateVAD = useCallback((level: number) => {
    const holder = vadHolderRef.current
    if (!holder) return
    const bars = Array.from(holder.querySelectorAll('.vb')) as HTMLDivElement[]
    bars.forEach((b, i) => {
      const c = (bars.length - 1) / 2
      const d = Math.abs(i - c) / c
      const h = isListeningRef.current ? Math.max(3, level * (1 - d * 0.55) * 18) : 3
      b.style.height = `${h}px`
      b.style.opacity = isListeningRef.current ? String(0.35 + level * 0.65) : '0'
    })
  }, [vadHolderRef])

  const stopMic = useCallback(() => {
    if (!isListeningRef.current) return
    isListeningRef.current = false
    cbRef.current.setIsListening(false)
    if (vadTimerRef.current) window.clearInterval(vadTimerRef.current)
    updateVAD(0)
    partialSendCountRef.current = 0
    const mr = mediaRecorderRef.current
    if (mr && mr.state !== 'inactive') mr.stop()
    micStreamRef.current?.getTracks().forEach((t) => t.stop())
    void audioCtxNodeRef.current?.close()
    cbRef.current.setMode('thinking')
    cbRef.current.setLog('processing...')
    cbRef.current.setPartial('')
  }, [updateVAD])

  const sendAudio = useCallback(async () => {
    if (!audioChunksRef.current.length) return
    cbRef.current.setBusy(true)
    const blob = new Blob(audioChunksRef.current, { type: 'audio/webm' })
    const buf = await blob.arrayBuffer()
    const bytes = new Uint8Array(buf)
    cbRef.current.sendWs({ type: 'audio', data: encodeBase64(bytes) })
  }, [])

  const startMic = useCallback(async () => {
    cbRef.current.ensureAudioContext()
    try {
      micStreamRef.current = await navigator.mediaDevices.getUserMedia({ audio: { sampleRate: 16000 } })
      audioCtxNodeRef.current = new AudioContext({ sampleRate: 16000 })
      analyserRef.current = audioCtxNodeRef.current.createAnalyser()
      analyserRef.current.fftSize = 64
      audioCtxNodeRef.current.createMediaStreamSource(micStreamRef.current).connect(analyserRef.current)

      vadTimerRef.current = window.setInterval(() => {
        const d = new Uint8Array(analyserRef.current?.frequencyBinCount ?? 0)
        analyserRef.current?.getByteFrequencyData(d)
        const level = d.length ? d.reduce((a, b) => a + b, 0) / d.length / 128 : 0
        updateVAD(level)
        if (isListeningRef.current) {
          if (level < VAD_SILENCE_LEVEL) silenceMsRef.current += VAD_INTERVAL_MS
          else silenceMsRef.current = 0
          if (Date.now() - listenStartedAtRef.current > MIN_RECORD_MS && silenceMsRef.current >= AUTO_ENDPOINT_MS) {
            stopMic()
          }
        }
      }, VAD_INTERVAL_MS)

      mediaRecorderRef.current = new MediaRecorder(micStreamRef.current)
      audioChunksRef.current = []
      partialChunksRef.current = []
      partialSendCountRef.current = 0

      mediaRecorderRef.current.ondataavailable = (e) => {
        if (e.data.size <= 0) return
        audioChunksRef.current.push(e.data)
        partialChunksRef.current.push(e.data)
        partialSendCountRef.current += 1
        if (partialSendCountRef.current >= PARTIAL_SEND_INTERVAL && partialSendCountRef.current % PARTIAL_SEND_INTERVAL === 0) {
          const blob = new Blob(partialChunksRef.current, { type: 'audio/webm' })
          void blob.arrayBuffer().then((buf) => {
            const bytes = new Uint8Array(buf)
            cbRef.current.sendWs({ type: 'audio_chunk', data: encodeBase64(bytes) })
          })
        }
      }

      mediaRecorderRef.current.onstop = () => { void sendAudio() }
      mediaRecorderRef.current.start(100)
      silenceMsRef.current = 0
      listenStartedAtRef.current = Date.now()
      cbRef.current.setIsListening(true)
      cbRef.current.setBusy(false)
      cbRef.current.setMode('listening')
      cbRef.current.setLog('listening')
    } catch (e) {
      const err = e as { name?: string; message?: string }
      if (err.name === 'NotAllowedError' || err.name === 'PermissionDeniedError') {
        cbRef.current.setLog('Microphone access denied - allow mic permission and refresh', 'err')
      } else {
        cbRef.current.setLog(`mic: ${err.message ?? 'unknown error'}`, 'err')
      }
    }
  }, [updateVAD, stopMic, sendAudio])

  const toggleMic = useCallback(async () => {
    cbRef.current.resetIdleTimer()
    if (isListeningRef.current) {
      stopMic()
      return
    }
    await startMic()
  }, [stopMic, startMic])

  return {
    toggleMic,
    stopMic,
    startMic,
    isListeningRef,
  }
}
