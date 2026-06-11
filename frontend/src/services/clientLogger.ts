export type ClientLogLevel = 'info' | 'warning' | 'error'

export type ClientLogPayload = {
  level: ClientLogLevel
  source: string
  message: string
  detail?: unknown
  turn_id?: string | null
}

export function writeClientLog(payload: ClientLogPayload): void {
  const args = [`[${payload.source}] ${payload.message}`, payload.detail].filter((item) => item !== undefined)
  if (payload.level === 'error') console.error(...args)
  else if (payload.level === 'warning') console.warn(...args)
  else console.info(...args)
}

