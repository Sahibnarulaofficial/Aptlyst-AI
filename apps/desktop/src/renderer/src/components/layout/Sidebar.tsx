import { NavLink, useLocation } from 'react-router-dom'
import { PanelLeftClose, PanelLeftOpen, Circle } from 'lucide-react'

import { navigation } from '@renderer/lib/navigation'
import { cn } from '@renderer/lib/cn'
import { useUIStore } from '@renderer/app/store/uiStore'

import { Tooltip } from '@renderer/components/ui/Tooltip'

export function Sidebar() {
  const location = useLocation()

  const sidebarCollapsed = useUIStore((state) => state.sidebarCollapsed)

  const toggleSidebar = useUIStore((state) => state.toggleSidebar)

  return (
    <aside className="flex h-full flex-col bg-zinc-950 text-white">
      {/* ================= Header ================= */}
      <div className="border-b border-zinc-800 p-4">
        <div
          className={cn(
            'flex items-center',
            sidebarCollapsed ? 'justify-center' : 'justify-between'
          )}
        >
          {!sidebarCollapsed ? (
            <div className="flex items-center gap-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-zinc-800">
                <Circle size={14} className="fill-emerald-500 text-emerald-500" />
              </div>

              <div>
                <h1 className="text-lg font-bold tracking-tight">Aptlyst AI</h1>

                <p className="text-xs text-zinc-400">Meeting Copilot</p>
              </div>
            </div>
          ) : (
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-zinc-800">
              <Circle size={14} className="fill-emerald-500 text-emerald-500" />
            </div>
          )}
        </div>

        <button
          onClick={toggleSidebar}
          className={cn(
            'mt-4 flex h-10 w-full items-center rounded-lg border border-zinc-800 text-zinc-400 transition-all duration-200 hover:bg-zinc-900 hover:text-white',
            sidebarCollapsed ? 'justify-center' : 'justify-start px-3'
          )}
        >
          {sidebarCollapsed ? (
            <PanelLeftOpen size={18} />
          ) : (
            <>
              <PanelLeftClose size={18} />

              <span className="ml-3 text-sm">Collapse</span>
            </>
          )}
        </button>
      </div>

      {/* ================= Navigation ================= */}
      <nav className="flex-1 px-3 py-4">
        <ul className="space-y-1">
          {navigation.map((item) => {
            const Icon = item.icon

            const isActive = location.pathname === item.path

            return (
              <li key={item.id}>
                <Tooltip content={item.title} disabled={!sidebarCollapsed}>
                  <NavLink
                    to={item.path}
                    className={cn(
                      'relative flex items-center rounded-xl transition-all duration-200',
                      sidebarCollapsed ? 'justify-center px-0 py-3' : 'gap-3 px-3 py-3',
                      isActive
                        ? 'bg-zinc-800 text-white'
                        : 'text-zinc-400 hover:bg-zinc-900 hover:text-white'
                    )}
                  >
                    {isActive && (
                      <span className="absolute left-0 h-8 w-1 rounded-r-full bg-emerald-500" />
                    )}

                    <Icon size={20} className="shrink-0" />

                    {!sidebarCollapsed && (
                      <span className="truncate text-sm font-medium">{item.title}</span>
                    )}
                  </NavLink>
                </Tooltip>
              </li>
            )
          })}
        </ul>
      </nav>

      {/* ================= Footer ================= */}
      <div className="border-t border-zinc-800 p-4">
        {!sidebarCollapsed ? (
          <>
            <div className="flex items-center gap-2">
              <Circle size={10} className="fill-emerald-500 text-emerald-500" />

              <span className="text-sm font-medium">Local AI</span>
            </div>

            <p className="mt-2 text-xs text-zinc-500">Offline</p>

            <p className="mt-1 text-xs text-zinc-600">v0.1.0-dev</p>
          </>
        ) : (
          <div className="flex justify-center">
            <Circle size={12} className="fill-emerald-500 text-emerald-500" />
          </div>
        )}
      </div>
    </aside>
  )
}
