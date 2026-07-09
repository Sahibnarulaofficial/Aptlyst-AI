import { Cpu, Wifi } from 'lucide-react'

export function StatusBar() {
  return (
    <div className="flex h-full items-center justify-between px-4 text-xs text-zinc-500">
      <div className="flex items-center gap-2">
        <Cpu size={14} />
        <span>Local AI Ready</span>
      </div>

      <div className="flex items-center gap-2">
        <Wifi size={14} />
        <span>Offline</span>
      </div>
    </div>
  )
}
