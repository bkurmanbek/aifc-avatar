import type { ChatMessage, StructuredAnswer, SupportedLanguage } from '../types'
import { uid } from '../utils'

export type ConversationState = {
  messages: ChatMessage[]
  aiMessageId: string | null
  activeAnswer: StructuredAnswer | null
  answerLanguage: SupportedLanguage
  activeSpokenChunk: number | null
  assistantStreamBuffer: string
  answerPayloadReceived: boolean
}

export type ConversationAction =
  | { type: 'user_message'; text: string; language: SupportedLanguage }
  | { type: 'response_start' }
  | { type: 'response_chunk'; text: string }
  | { type: 'answer_payload'; answer: StructuredAnswer; formattedText: string }
  | { type: 'done' }
  | { type: 'reset' }
  | { type: 'interrupted' }
  | { type: 'set_answer_language'; language: SupportedLanguage }
  | { type: 'active_spoken_chunk'; chunk: number | null }

export const initialConversationState: ConversationState = {
  messages: [],
  aiMessageId: null,
  activeAnswer: null,
  answerLanguage: 'en',
  activeSpokenChunk: null,
  assistantStreamBuffer: '',
  answerPayloadReceived: false,
}

export function conversationReducer(
  state: ConversationState,
  action: ConversationAction,
): ConversationState {
  switch (action.type) {
    case 'user_message':
      return {
        ...state,
        answerLanguage: action.language,
        messages: [
          ...state.messages.filter((m) => m.id !== 'typing'),
          { id: uid(), role: 'user', text: action.text },
          { id: 'typing', role: 'avatar', text: '__typing__' },
        ],
      }

    case 'response_start': {
      const id = uid()
      return {
        ...state,
        aiMessageId: id,
        activeAnswer: null,
        activeSpokenChunk: null,
        assistantStreamBuffer: '',
        answerPayloadReceived: false,
        messages: [
          ...state.messages.filter((m) => m.id !== 'typing'),
          { id, role: 'avatar', text: '' },
        ],
      }
    }

    case 'response_chunk': {
      const id = state.aiMessageId ?? uid()
      const newBuffer = state.assistantStreamBuffer + action.text
      const hasCurrent = state.messages.some((m) => m.id === id)
      const base = hasCurrent
        ? state.messages.filter((m) => m.id !== 'typing')
        : [
            ...state.messages.filter((m) => m.id !== 'typing'),
            { id, role: 'avatar' as const, text: '' },
          ]
      return {
        ...state,
        aiMessageId: id,
        assistantStreamBuffer: newBuffer,
        messages: base.map((m) => (m.id === id ? { ...m, text: newBuffer } : m)),
      }
    }

    case 'answer_payload': {
      // Write the final answer text onto the active avatar message. If there is no active
      // message id yet (e.g. intro / FAQ-video turns where the payload can arrive before a
      // response_chunk created one, or a 'done' raced ahead and cleared the id), do NOT
      // drop the text — fall back to the last avatar message, or append a fresh one. This
      // guarantees intro + FAQ answers always render in the chat.
      const id = state.aiMessageId
      const text = action.formattedText
      const base = state.messages.filter((m) => m.id !== 'typing')
      let messages = base
      if (id && base.some((m) => m.id === id)) {
        messages = base.map((m) => (m.id === id ? { ...m, text } : m))
      } else {
        // Reuse the trailing avatar bubble if the stream already created one; else append.
        const lastAvatarIdx = [...base].reverse().findIndex((m) => m.role === 'avatar')
        if (lastAvatarIdx !== -1) {
          const realIdx = base.length - 1 - lastAvatarIdx
          messages = base.map((m, i) => (i === realIdx ? { ...m, text } : m))
        } else if (text.trim()) {
          messages = [...base, { id: id ?? uid(), role: 'avatar' as const, text }]
        }
      }
      return {
        ...state,
        activeAnswer: action.answer,
        answerPayloadReceived: true,
        assistantStreamBuffer: text,
        messages,
      }
    }

    case 'done': {
      const shouldFallback = !state.answerPayloadReceived && state.aiMessageId && state.assistantStreamBuffer.trim()
      return {
        ...state,
        messages: shouldFallback
          ? state.messages.map((m) => (
              m.id === state.aiMessageId ? { ...m, text: state.assistantStreamBuffer.trim() } : m
            ))
          : state.messages,
        aiMessageId: null,
        assistantStreamBuffer: '',
      }
    }

    case 'interrupted':
      return {
        ...state,
        aiMessageId: null,
        activeSpokenChunk: null,
        assistantStreamBuffer: '',
      }

    case 'reset':
      return initialConversationState

    case 'set_answer_language':
      return { ...state, answerLanguage: action.language }

    case 'active_spoken_chunk':
      return { ...state, activeSpokenChunk: action.chunk }

    default:
      return state
  }
}
