import React from 'react'

import { cn } from '@renderer/lib/cn'
import { useUIStore } from '@renderer/app/store/uiStore'

type AppShellProps = {
  sidebar: React.ReactNode
  header: React.ReactNode
  workspace: React.ReactNode
  statusBar: React.ReactNode
}

export function AppShell({ sidebar, header, workspace, statusBar }: AppShellProps) {
  const sidebarCollapsed = useUIStore((state) => state.sidebarCollapsed)

  return (
    <div
      className={cn(
        'grid h-screen overflow-hidden bg-zinc-100 transition-[grid-template-columns] duration-300',
        sidebarCollapsed ? 'grid-cols-[72px_1fr]' : 'grid-cols-[260px_1fr]',
        'grid-rows-[64px_1fr_32px]'
      )}
    >
      <aside className="row-span-3 border-r border-zinc-200 bg-zinc-950">{sidebar}</aside>

      <header className="border-b border-zinc-200 bg-white">{header}</header>

      <main className="overflow-auto bg-zinc-100">{workspace}</main>

      <footer className="border-t border-zinc-200 bg-white">{statusBar}</footer>
    </div>
  )
}
