import { Bell, Search, Sparkles } from 'lucide-react'
import { useLocation } from 'react-router-dom'

import { getCurrentNavigation } from '@renderer/lib/navigationHelpers'

export function Header() {
  const location = useLocation()

  const page = getCurrentNavigation(location.pathname)

  return (
    <div className="flex h-full items-center justify-between px-6">
      {/* Left */}
      <div>
        <h1 className="text-xl font-semibold text-zinc-900">{page.title}</h1>

        <p className="text-sm text-zinc-500">{page.description}</p>
      </div>

      {/* Right */}
      <div className="flex items-center gap-3">
        <button className="rounded-lg border border-zinc-200 p-2 transition hover:bg-zinc-100">
          <Search size={18} />
        </button>

        <div className="flex items-center gap-2 rounded-full border border-zinc-200 px-3 py-1.5">
          <Sparkles size={16} className="text-emerald-500" />

          <span className="text-sm font-medium">'Local AI'</span>
        </div>

        <button className="rounded-lg border border-zinc-200 p-2 transition hover:bg-zinc-100">
          <Bell size={18} />
        </button>

        <div className="flex h-10 w-10 items-center justify-center rounded-full bg-zinc-900 font-semibold text-white">
          S
        </div>
      </div>
    </div>
  )
}
