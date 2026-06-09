import { useEffect, useRef } from 'react'
import type { ChangeEvent, KeyboardEvent } from 'react'

interface FloatingChatComposerProps {
  open: boolean
  value: string
  placeholder?: string
  busy?: boolean
  onChange: (value: string) => void
  onSubmit: () => void
  onClose: () => void
}

export function FloatingChatComposer({
  open,
  value,
  placeholder = 'Ask me anything…',
  busy = false,
  onChange,
  onSubmit,
  onClose,
}: FloatingChatComposerProps) {
  const cardRef = useRef<HTMLDivElement | null>(null)
  const inputRef = useRef<HTMLInputElement | null>(null)

  useEffect(() => {
    if (!open) return
    const id = requestAnimationFrame(() => inputRef.current?.focus())
    return () => cancelAnimationFrame(id)
  }, [open])

  useEffect(() => {
    if (!open) return
    const onKeyDown = (e: globalThis.KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    const onPointerDown = (e: PointerEvent) => {
      const t = e.target as Node | null
      if (t && cardRef.current && !cardRef.current.contains(t)) onClose()
    }
    document.addEventListener('keydown', onKeyDown)
    document.addEventListener('pointerdown', onPointerDown, true)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      document.removeEventListener('pointerdown', onPointerDown, true)
    }
  }, [onClose, open])

  const handleKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') { e.preventDefault(); onSubmit() }
  }

  const canSend = Boolean(value.trim()) && !busy

  return (
    <div className={`fcc-shell ${open ? 'open' : ''}`} aria-hidden={!open}>
      <div className="fcc-card" ref={cardRef} role="dialog" aria-label="Ask AIFC">
        <div className="fcc-row">
          <span className="fcc-icon" aria-hidden="true">
            <svg width="19" height="19" viewBox="0 0 24 24" fill="currentColor">
              <path d="M12 2l1.6 5.1L19 9l-5.4 1.9L12 16l-1.6-5.1L5 9l5.4-1.9L12 2z" />
              <path d="M19 14l.8 2.4L22 17l-2.2.6L19 20l-.8-2.4L16 17l2.2-.6L19 14z" />
            </svg>
          </span>

          <input
            ref={inputRef}
            className="fcc-input"
            type="text"
            value={value}
            onChange={(e: ChangeEvent<HTMLInputElement>) => onChange(e.target.value)}
            onKeyDown={handleKey}
            placeholder={placeholder}
            aria-label="Ask a question"
            autoComplete="off"
            spellCheck={false}
          />

          {busy ? (
            <span className="fcc-busy" aria-label="Responding…">
              <span /><span /><span />
            </span>
          ) : (
            <button
              className={`fcc-send ${canSend ? 'ready' : ''}`}
              type="button"
              onClick={onSubmit}
              disabled={!canSend}
              aria-label="Send message"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <line x1="12" y1="19" x2="12" y2="5" />
                <polyline points="5 12 12 5 19 12" />
              </svg>
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
