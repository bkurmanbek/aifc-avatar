export type UiMode = 'idle' | 'listening' | 'absorbing' | 'composing' | 'thinking' | 'rendering' | 'speaking'
export type SupportedLanguage = 'en' | 'ru' | 'kk' | 'zh'

export type WsInbound =
  | { type: 'partial'; text: string }
  | { type: 'transcript'; text: string; session_id?: string }
  | { type: 'transcript_empty'; text?: string; session_id?: string }
  | { type: 'stt_ready'; ready_ms?: number; session_id?: string }
  | { type: 'session_state'; session_id: string; state: string }
  | { type: 'response_start'; turn_id?: string }
  | { type: 'response_chunk'; text: string; turn_id?: string }
  | { type: 'answer_payload'; turn_id?: string; answer_id: string; spoken: string; chat: string }
  | { type: 'policy_state'; answer_language?: string | null; turn_id?: string }
  | { type: 'audio_ready'; data?: string; audio_url?: string; chunk?: number; frame_stride?: number; turn_id?: string; cached?: boolean; expected_frames?: number }
  | { type: 'frame_cache'; url: string; chunk?: number; turn_id?: string; frame_count?: number }
  | { type: 'intro_video'; url: string; turn_id?: string }
  | { type: 'frame'; data: string; chunk?: number; turn_id?: string }
  | { type: 'chunk_done'; chunk?: number; turn_id?: string }
  | { type: 'media_error'; text: string; chunk?: number; turn_id?: string }
  | { type: 'done'; chunks?: number; turn_id?: string; latency_ms?: Record<string, number | string> }
  | { type: 'status'; text: string; turn_id?: string }
  | { type: 'interrupted'; session_id?: string }
  | { type: 'stop_confirmed' }
  | { type: 'error'; text: string; turn_id?: string }

export type ChatRole = 'user' | 'avatar'

export type StructuredAnswer = {
  answer_id: string
  spoken: string
  chat: string
}

export type ChatMessage = {
  id: string
  role: ChatRole
  text: string
}

export type ChunkState = {
  audio: string | null       // base64 WAV (Q&A path) or '__url__' sentinel (intro path)
  rawBuffer?: ArrayBuffer    // raw WAV bytes from HTTP fetch (intro path — avoids atob cost)
  decodedAudio?: AudioBuffer // pre-decoded AudioBuffer (used when available)
  frames: (string | Blob)[]  // base64 JPEG (cached/intro JSON path) or Blob (binary WS frames)
  bitmapCache: Record<number, ImageBitmap>  // GPU-ready bitmaps decoded off-thread
  bitmapPending: Set<number>                // frame indices currently being decoded
  frameDone: boolean
  frameStride: number
  expectedFrames?: number
  cached?: boolean
  frameCacheLoading?: boolean
  error?: boolean
  turnId?: string
  // Gapless audio scheduling: each chunk's audio is queued onto the AudioContext
  // timeline back-to-back so mid-sentence segments play without clicks.
  decoding?: boolean
  scheduledSource?: AudioBufferSourceNode
  scheduledT0?: number        // AudioContext time this chunk's audio starts
  scheduledDuration?: number  // decoded buffer duration (seconds)
}
