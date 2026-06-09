import { useEffect, useRef } from 'react'
import { AssistantMarkdown } from './AssistantMarkdown'
import type { ChatMessage } from '../types'

interface ChatPanelProps {
  messages: ChatMessage[]
  partialText: string
  isListening: boolean
  aiMessageId: string | null
}

export function ChatPanel({ messages, partialText, isListening, aiMessageId }: ChatPanelProps) {
  const feedRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    const feed = feedRef.current
    if (!feed) return
    feed.scrollTop = feed.scrollHeight
    const frame = window.requestAnimationFrame(() => {
      feed.scrollTop = feed.scrollHeight
    })
    return () => window.cancelAnimationFrame(frame)
  }, [messages, partialText, isListening])

  return (
    <section className="chat-panel" aria-label="Conversation transcript">
      <div className="chat-body" id="chat-body">
        <div className="chat-feed" ref={feedRef}>
          {messages.length === 0 && !(isListening && partialText) && (
            <div className="chat-empty">
              <span className="chat-empty-mark" aria-hidden="true">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M7 17 17 7" />
                  <path d="M9 7h8v8" />
                </svg>
              </span>
              <p>Ready for conversation</p>
            </div>
          )}
          {messages.map((m) =>
            m.id === 'typing' ? (
              <div className="msg a msg-streaming" key="typing">
                <div className="mlbl">Assistant</div>
                <div className="mtext">
                  <div className="typing-dots">
                    <div className="tdot" />
                    <div className="tdot" />
                    <div className="tdot" />
                  </div>
                </div>
              </div>
            ) : (
              <div className={`msg ${m.role === 'user' ? 'u' : 'a'} ${m.id === aiMessageId ? 'msg-streaming' : ''}`} key={m.id}>
                <div className="mlbl">{m.role === 'user' ? 'You' : 'Assistant'}</div>
                <div className="mtext">
                  {m.role === 'avatar' ? <AssistantMarkdown text={m.text} /> : <span>{m.text}</span>}
                  {m.id === aiMessageId && m.text ? <span className="cur" /> : null}
                </div>
              </div>
            ),
          )}
          {isListening && partialText && (
            <div className="msg u msg-partial" key="partial-live">
              <div className="mlbl">You</div>
              <div className="mtext">{partialText}</div>
            </div>
          )}
        </div>
      </div>
    </section>
  )
}
