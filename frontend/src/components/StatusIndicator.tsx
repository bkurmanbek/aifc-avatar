import { dotClass } from '../utils'

function labelForMode(mode: string, statusText: string): string {
  if (mode === 'listening') return 'Listening'
  if (mode === 'thinking') return 'Thinking'
  if (mode === 'rendering') return 'Rendering'
  if (mode === 'speaking') return 'Speaking'
  if (mode === 'idle') return 'Ready'
  return statusText || mode
}

export function StatusIndicator({ mode, statusText }: { mode: string; statusText: string }) {
  return (
    <div className={`status-indicator ${mode}`} aria-live="polite">
      <div className={dotClass(mode)} role="img" aria-label={`Status: ${mode}`} />
      <span>{mode === 'idle' ? 'Live' : labelForMode(mode, statusText)}</span>
    </div>
  )
}

export function ThemeToggle({ darkMode, onToggle }: { darkMode: boolean; onToggle: () => void }) {
  return (
    <button
      className="theme-toggle"
      onClick={onToggle}
      aria-label={darkMode ? 'Switch to light mode' : 'Switch to dark mode'}
    >
      {darkMode ? (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <circle cx="12" cy="12" r="5"/>
          <line x1="12" y1="1" x2="12" y2="3"/>
          <line x1="12" y1="21" x2="12" y2="23"/>
          <line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/>
          <line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/>
          <line x1="1" y1="12" x2="3" y2="12"/>
          <line x1="21" y1="12" x2="23" y2="12"/>
          <line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/>
          <line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>
        </svg>
      ) : (
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
        </svg>
      )}
    </button>
  )
}
