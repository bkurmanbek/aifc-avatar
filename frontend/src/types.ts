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
  | { type: 'answer_payload'; turn_id?: string; answer_id: string; spoken: string; details: AnswerDetails; key_points: AnswerKeyPoint[]; follow_up_questions: string[] }
  | { type: 'policy_state'; answer_language?: string | null; turn_id?: string }
  | { type: 'audio_ready'; data: string; chunk?: number; frame_stride?: number; turn_id?: string; cached?: boolean }
  | { type: 'frame_cache'; url: string; chunk?: number; turn_id?: string; frame_count?: number }
  | { type: 'frame'; data: string; chunk?: number; turn_id?: string }
  | { type: 'chunk_done'; chunk?: number; turn_id?: string }
  | { type: 'media_error'; text: string; chunk?: number; turn_id?: string }
  | { type: 'done'; chunks?: number; turn_id?: string; latency_ms?: Record<string, number | string> }
  | { type: 'status'; text: string; turn_id?: string }
  | { type: 'interrupted'; session_id?: string }
  | { type: 'stop_confirmed' }
  | { type: 'error'; text: string; turn_id?: string }

export type ChatRole = 'user' | 'avatar'

export type AnswerSection = {
  id: string
  title: string
  items: string[]
  text: string
}

export type AnswerDetails = {
  summary: string
  sections: AnswerSection[]
}

export type AnswerKeyPoint = {
  id: string
  label: string
  preview: string
  section_index: number
}

export type StructuredAnswer = {
  answer_id: string
  spoken: string
  details: AnswerDetails
  key_points: AnswerKeyPoint[]
  follow_up_questions: string[]
}

export type ChatMessage = {
  id: string
  role: ChatRole
  text: string
}

export type ChunkState = {
  audio: string | null
  frames: string[]
  frameDone: boolean
  frameStride: number
  cached?: boolean
  frameCacheLoading?: boolean
  error?: boolean
  turnId?: string
}
