import { useCallback, useEffect, useRef } from 'react'

export function useIdleTimer(timeoutMs: number, onTimeout: () => void) {
  const timerRef = useRef<number | null>(null)
  const onTimeoutRef = useRef(onTimeout)

  useEffect(() => {
    onTimeoutRef.current = onTimeout
  }, [onTimeout])

  const reset = useCallback(() => {
    if (timerRef.current) window.clearTimeout(timerRef.current)
    timerRef.current = window.setTimeout(() => onTimeoutRef.current(), timeoutMs)
  }, [timeoutMs])

  const clear = useCallback(() => {
    if (timerRef.current) window.clearTimeout(timerRef.current)
    timerRef.current = null
  }, [])

  useEffect(() => clear, [clear])

  return { reset, clear }
}
