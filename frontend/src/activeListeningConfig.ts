function readNumber(name: string, fallback: number, min: number, max: number): number {
  const raw = import.meta.env[name] as string | undefined
  const value = raw == null || raw.trim() === '' ? fallback : Number(raw)
  if (!Number.isFinite(value)) return fallback
  return Math.min(max, Math.max(min, value))
}

function readBoolean(name: string, fallback: boolean): boolean {
  const raw = (import.meta.env[name] as string | undefined)?.trim().toLowerCase()
  if (raw == null || raw === '') return fallback
  if (['1', 'true', 'yes', 'on'].includes(raw)) return true
  if (['0', 'false', 'no', 'off'].includes(raw)) return false
  return fallback
}

export const activeListeningConfig = {
  enabled: readBoolean('VITE_ACTIVE_LISTENING_ENABLED', true),
  positiveSpeechThreshold: readNumber('VITE_ACTIVE_POSITIVE_SPEECH_THRESHOLD', 0.35, 0.05, 0.95),
  negativeSpeechThreshold: readNumber('VITE_ACTIVE_NEGATIVE_SPEECH_THRESHOLD', 0.28, 0.01, 0.9),
  redemptionMs: readNumber('VITE_ACTIVE_FINAL_SILENCE_MS', 800, 500, 6000),
  minSpeechMs: readNumber('VITE_ACTIVE_MIN_RECORD_MS', 700, 500, 5000),
  preRollMs: readNumber('VITE_ACTIVE_PREROLL_MS', 800, 0, 3000),
  model: ((import.meta.env.VITE_ACTIVE_VAD_MODEL as string | undefined) === 'v5' ? 'v5' : 'legacy') as 'legacy' | 'v5',
}
