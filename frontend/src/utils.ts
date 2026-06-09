export function uid(): string {
  return Math.random().toString(36).slice(2)
}

const KK_HINT_RE = /(\d[\d\s,./-]*)\s*\[([^\][]+)]/g

export function stripSpeechHints(text: string): string {
  return text.replace(KK_HINT_RE, '$1')
}

export function dotClass(mode: string): string {
  if (mode === 'listening') return 'dot lst'
  if (mode === 'thinking') return 'dot thk'
  if (mode === 'rendering') return 'dot rnd'
  if (mode === 'speaking') return 'dot spk'
  return 'dot on'
}

export function encodeBase64(bytes: Uint8Array): string {
  const CHUNK_SIZE = 0x2000
  let binary = ''
  for (let i = 0; i < bytes.length; i += CHUNK_SIZE) {
    binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK_SIZE))
  }
  return btoa(binary)
}
