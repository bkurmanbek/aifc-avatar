interface StatusBarProps {
  logText: string
  logClass: string
}

export function StatusBar({ logText, logClass }: StatusBarProps) {
  return (
    <footer className="status-bar" role="status" aria-live="polite">
      <span className={`log-text ${logClass}`}>{logText}</span>
      <span className="status-year" aria-hidden="true">AIFC · 2026</span>
    </footer>
  )
}
