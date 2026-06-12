import type { ReactNode } from 'react'

export function SidebarCard({
  title,
  children,
  className = '',
}: {
  title: string
  children: ReactNode
  className?: string
}) {
  return (
    <aside className={`sidebar-card ${className}`} aria-label={title}>
      <div className="sidebar-card-hdr">
        <span>{title}</span>
        <span className="panel-dot" aria-hidden="true" />
      </div>
      <div className="sidebar-card-body">{children}</div>
    </aside>
  )
}
