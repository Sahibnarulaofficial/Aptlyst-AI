import { navigation } from '@renderer/lib/navigation'

export function Sidebar() {
  return (
    <div className="flex h-full flex-col">
      {/* Logo */}
      <div className="border-b border-zinc-800 p-6">
        <h1 className="text-2xl font-bold tracking-tight text-white">Aptlyst AI</h1>

        <p className="mt-1 text-xs text-zinc-400">Meeting Copilot</p>
      </div>

      {/* Navigation */}
      <nav className="flex-1 p-3">
        <ul className="space-y-1">
          {navigation.map((item) => {
            const Icon = item.icon

            return (
              <li key={item.title}>
                <button className="flex w-full items-center gap-3 rounded-lg px-3 py-2 text-sm text-zinc-300 transition hover:bg-zinc-900 hover:text-white">
                  <Icon size={18} />

                  <span>{item.title}</span>
                </button>
              </li>
            )
          })}
        </ul>
      </nav>

      {/* Footer */}
      <div className="border-t border-zinc-800 p-4">
        <div className="text-xs text-zinc-500">Local AI Ready</div>
      </div>
    </div>
  )
}
