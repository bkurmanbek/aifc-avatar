import type { SupportedLanguage } from './types'

export function uid(): string {
  return Math.random().toString(36).slice(2)
}

const KK_HINT_RE = /(\d[\d\s,./-]*)\s*\[([^\][]+)]/g

export function stripSpeechHints(text: string): string {
  return text.replace(KK_HINT_RE, '$1')
}

export function encodeBase64(bytes: Uint8Array): string {
  const CHUNK_SIZE = 0x2000
  let binary = ''
  for (let i = 0; i < bytes.length; i += CHUNK_SIZE) {
    binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK_SIZE))
  }
  return btoa(binary)
}

export function detectUiLanguage(text: string): SupportedLanguage {
  const lower = text.toLowerCase()
  if (/[\u4e00-\u9fff]/.test(lower)) return 'zh'
  if (/[әғқңөұүһі]/.test(lower) || /(сәлем|рахмет|рақмет|қалай|жоқ|иә|жұмыс|құжат)/.test(lower)) return 'kk'
  if (/[а-яё]/.test(lower)) return 'ru'
  return 'en'
}
