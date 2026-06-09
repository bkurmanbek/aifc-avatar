import type { KeyboardEvent, ChangeEvent } from 'react'

interface DockControlsProps {
  isListening: boolean
  isBusy: boolean
  showChat: boolean
  inputText: string
  onSendText: () => void
  onInputChange: (text: string) => void
  onToggleChat: () => void
}

export function DockControls({
  isListening,
  isBusy,
  showChat,
  inputText,
  onSendText,
  onInputChange,
  onToggleChat,
}: DockControlsProps) {
  return (
    <section className="dock-wrap">
      <div className="controls dock-controls">
        <div className="dock-input-area chat-input-area">
          <div className={`dock-state-strip ${isListening ? 'active' : ''}`} aria-hidden="true">
            <span />
            <span />
            <span />
            <span />
            <span />
            <span />
            <span />
          </div>
          <div className="input-row dock-input-row">
            <input
              className="txt-in"
              type="text"
              value={inputText}
              onChange={(e: ChangeEvent<HTMLInputElement>) => onInputChange(e.target.value)}
              onKeyDown={(e: KeyboardEvent<HTMLInputElement>) => { if (e.key === 'Enter') onSendText() }}
              placeholder={isListening ? 'Listening...' : isBusy ? 'Assistant is responding...' : 'Ask AIFC anything...'}
              aria-label="Text message input"
            />
            <button
              className="send-btn"
              onClick={onSendText}
              disabled={isBusy || !inputText.trim()}
              aria-label="Send message"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <line x1="12" y1="19" x2="12" y2="5"/>
                <polyline points="5 12 12 5 19 12"/>
              </svg>
            </button>
          </div>
        </div>

        <div className="chat-toggle-stack">
          <button
            className={`chat-toggle-btn${showChat ? ' active' : ''}`}
            onClick={onToggleChat}
            aria-pressed={showChat}
            aria-label={showChat ? 'Hide conversation' : 'Show conversation'}
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
            </svg>
          </button>
          <span className="chat-toggle-lbl">Chat</span>
        </div>
      </div>
    </section>
  )
}
