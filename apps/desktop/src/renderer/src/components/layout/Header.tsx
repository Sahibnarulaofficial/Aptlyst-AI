import { Bell, Search, Sparkles } from 'lucide-react'

export function Header() {
  return (
    <div className="flex h-full items-center justify-between px-6">
      {/* Left */}
      <div>
        <h2 className="text-xl font-semibold text-zinc-900">Dashboard</h2>

        <p className="text-sm text-zinc-500">Welcome to Aptlyst AI</p>
      </div>

      {/* Right */}
      <div className="flex items-center gap-3">
        {/* Search */}
        <button className="rounded-lg border p-2 transition hover:bg-zinc-100">
          <Search size={18} />
        </button>

        {/* AI Status */}
        <div className="flex items-center gap-2 rounded-full border px-3 py-1.5">
          <Sparkles size={16} className="text-emerald-500" />

          <span className="text-sm font-medium">Local AI</span>
        </div>

        {/* Notifications */}
        <button className="rounded-lg border p-2 transition hover:bg-zinc-100">
          <Bell size={18} />
        </button>

        {/* User */}
        <div className="flex h-10 w-10 items-center justify-center rounded-full bg-zinc-900 font-semibold text-white">
          S
        </div>
      </div>
    </div>
  )
}
