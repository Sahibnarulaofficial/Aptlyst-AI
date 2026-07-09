import { Command } from '../types'

export interface GroupedCommand {
  command: Command
  globalIndex: number
}

export interface CommandGroup {
  category: Command['category']
  items: GroupedCommand[]
}

export function groupCommands(commands: Command[]): CommandGroup[] {
  const groups = new Map<string, CommandGroup>()

  commands.forEach((command, index) => {
    const key = command.category.id

    if (!groups.has(key)) {
      groups.set(key, {
        category: command.category,
        items: []
      })
    }

    groups.get(key)!.items.push({
      command,
      globalIndex: index
    })
  })

  return [...groups.values()].sort((a, b) => a.category.order - b.category.order)
}
