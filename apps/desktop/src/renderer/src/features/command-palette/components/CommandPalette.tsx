import { useEffect, useMemo, useRef, useState } from 'react'
import { Search } from 'lucide-react'
import { useNavigate } from 'react-router-dom'

import { cn } from '@renderer/lib/cn'

import { commands } from '../registry/commands'
import { useCommandPaletteStore } from '../store/commandPaletteStore'
import { groupCommands } from '../utils/groupCommands'

export function CommandPalette() {
  const navigate = useNavigate()

  const open = useCommandPaletteStore((state) => state.open)
  const closePalette = useCommandPaletteStore((state) => state.closePalette)

  const [query, setQuery] = useState('')
  const [selectedIndex, setSelectedIndex] = useState(0)

  const itemRefs = useRef<(HTMLButtonElement | null)[]>([])

  const filteredCommands = useMemo(() => {
    if (!query.trim()) {
      return commands
    }

    const search = query.toLowerCase()

    return commands.filter(
      (command) =>
        command.title.toLowerCase().includes(search) ||
        command.description.toLowerCase().includes(search) ||
        command.category.title.toLowerCase().includes(search)
    )
  }, [query])

  const groupedCommands = useMemo(() => groupCommands(filteredCommands), [filteredCommands])

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (!open) return

      switch (e.key) {
        case 'Escape':
          e.preventDefault()
          closePalette()
          break

        case 'ArrowDown':
          e.preventDefault()

          setSelectedIndex((current) => Math.min(current + 1, filteredCommands.length - 1))
          break

        case 'ArrowUp':
          e.preventDefault()

          setSelectedIndex((current) => Math.max(current - 1, 0))
          break

        case 'Enter': {
          e.preventDefault()

          const command = filteredCommands[selectedIndex]

          if (!command) return

          if (command.path) {
            navigate(command.path)
          }

          closePalette()
          break
        }
      }
    }

    window.addEventListener('keydown', onKeyDown)

    return () => window.removeEventListener('keydown', onKeyDown)
  }, [open, filteredCommands, selectedIndex, navigate, closePalette])

  useEffect(() => {
    if (!open) {
      setQuery('')
      setSelectedIndex(0)
      itemRefs.current = []
    }
  }, [open])

  useEffect(() => {
    setSelectedIndex(0)
  }, [query])

  useEffect(() => {
    itemRefs.current[selectedIndex]?.scrollIntoView({
      block: 'nearest'
    })
  }, [selectedIndex])

  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/40 pt-24 backdrop-blur-sm"
      onClick={closePalette}
    >
      <div
        className="w-full max-w-xl overflow-hidden rounded-2xl border border-zinc-800 bg-zinc-950 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Search */}
        <div className="flex items-center gap-3 border-b border-zinc-800 px-4 py-4">
          <Search size={18} className="text-zinc-500" />

          <input
            autoFocus
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search commands..."
            className="w-full bg-transparent text-white outline-none placeholder:text-zinc-500"
          />
        </div>

        {/* Results */}
        <div className="max-h-96 overflow-y-auto p-2">
          {filteredCommands.length === 0 && (
            <div className="flex flex-col items-center justify-center py-10 text-center">
              <Search size={32} className="mb-3 text-zinc-600" />

              <p className="font-medium text-white">No commands found</p>

              <p className="mt-2 text-sm text-zinc-500">
                Try searching for Dashboard, Meeting or Settings.
              </p>
            </div>
          )}

          {groupedCommands.map((group) => (
            <div key={group.category.id} className="mb-4 first:mt-0">
              <h2 className="px-3 pb-2 text-xs font-semibold uppercase tracking-wider text-zinc-500">
                {group.category.title}
              </h2>

              <div className="space-y-1">
                {group.items.map(({ command, globalIndex }) => {
                  const Icon = command.icon

                  return (
                    <button
                      key={command.id}
                      ref={(element) => {
                        itemRefs.current[globalIndex] = element
                      }}
                      onMouseEnter={() => setSelectedIndex(globalIndex)}
                      onClick={() => {
                        if (command.path) {
                          navigate(command.path)
                        }

                        closePalette()
                      }}
                      className={cn(
                        'flex w-full items-center gap-3 rounded-xl px-3 py-3 text-left transition-colors',
                        selectedIndex === globalIndex
                          ? 'bg-zinc-800 text-white'
                          : 'hover:bg-zinc-900'
                      )}
                    >
                      <Icon size={18} className="text-zinc-400" />

                      <div className="flex-1">
                        <p className="font-medium text-white">{command.title}</p>

                        <p className="text-xs text-zinc-500">{command.description}</p>
                      </div>
                    </button>
                  )
                })}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
