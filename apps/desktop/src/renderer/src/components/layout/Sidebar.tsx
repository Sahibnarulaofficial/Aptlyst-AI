import { NavLink } from 'react-router-dom'
import { PanelLeftClose, PanelLeftOpen } from 'lucide-react'

import { navigation } from '@renderer/lib/navigation'
import { cn } from '@renderer/lib/cn'
import { useUIStore } from '@renderer/app/store/uiStore'

export function Sidebar() {
  const sidebarCollapsed = useUIStore((state) => state.sidebarCollapsed)

  const toggleSidebar = useUIStore((state) => state.toggleSidebar)

  return (
    <div className="flex h-full flex-col bg-zinc-950 text-white">
      {/* Header */}
      <div className="border-b border-zinc-800 p-4">
        <div
          className={cn(
            'flex items-center',
            sidebarCollapsed ? 'justify-center' : 'justify-between'
          )}
        >
          {!sidebarCollapsed && (
            <div>
              <h1 className="text-xl font-bold tracking-tight">Aptlyst AI</h1>

              <p className="mt-1 text-xs text-zinc-400">Meeting Copilot</p>
            </div>
          )}

          <button
            onClick={toggleSidebar}
            className="rounded-lg p-2 text-zinc-400 transition hover:bg-zinc-800 hover:text-white"
          >
            {sidebarCollapsed ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={18} />}
          </button>
        </div>
      </div>

      {/* Navigation */}
      <nav className="flex-1 p-3">
        <ul className="space-y-1">
          {navigation.map((item) => {
            const Icon = item.icon

            return (
              <li key={item.path}>
                <NavLink
                  to={item.path}
                  className={({ isActive }) =>
                    cn(
                      'flex items-center rounded-lg px-3 py-2 text-sm transition-colors duration-200',
                      sidebarCollapsed ? 'justify-center' : 'gap-3',
                      isActive
                        ? 'bg-zinc-800 text-white'
                        : 'text-zinc-400 hover:bg-zinc-900 hover:text-white'
                    )
                  }
                >
                  <Icon size={18} />

                  {!sidebarCollapsed && <span>{item.title}</span>}
                </NavLink>
              </li>
            )
          })}
        </ul>
      </nav>

      {/* Footer */}
      <div
        className={cn('border-t border-zinc-800 p-4', sidebarCollapsed && 'flex justify-center')}
      >
        {!sidebarCollapsed ? (
          <div>
            <p className="text-xs text-zinc-500">Local AI Ready</p>
          </div>
        ) : (
          <div className="h-2 w-2 rounded-full bg-emerald-500" title="Local AI Ready" />
        )}
      </div>
    </div>
  )
}
