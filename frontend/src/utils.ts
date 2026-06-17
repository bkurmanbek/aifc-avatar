import type { SupportedLanguage } from './types'

// Single knob for split deployments (e.g. frontend on Vercel, backend elsewhere).
// When set (e.g. https://api.example.com), all backend HTTP assets and the WebSocket
// are addressed there. When empty (local / single-origin behind one tunnel), URLs stay
// relative / same-origin and behaviour is unchanged.
export const BACKEND_ORIGIN = ((import.meta.env.VITE_BACKEND_ORIGIN as string | undefined)?.trim() || '').replace(/\/+$/, '')

// Resolve a backend-served HTTP path (e.g. "/intro-video/...") to an absolute URL when
// BACKEND_ORIGIN is configured; otherwise leave it relative (same-origin).
export function backendHttpUrl(path: string): string {
  if (!BACKEND_ORIGIN || /^https?:\/\//i.test(path)) return path
  return `${BACKEND_ORIGIN}${path.startsWith('/') ? '' : '/'}${path}`
}

// Derive the WebSocket URL from BACKEND_ORIGIN (https→wss, http→ws). Falls back to the
// page origin's /ws when no backend origin is configured.
export function backendWsUrl(path = '/ws'): string {
  if (!BACKEND_ORIGIN) {
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
    return `${proto}://${window.location.host}${path}`
  }
  return `${BACKEND_ORIGIN.replace(/^http/i, 'ws')}${path}`
}

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
