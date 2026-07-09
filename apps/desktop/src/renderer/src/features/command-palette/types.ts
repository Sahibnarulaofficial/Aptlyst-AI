import { LucideIcon } from 'lucide-react'

export interface CommandCategory {
  id: string
  title: string
  order: number
}

export interface Command {
  id: string
  title: string
  description: string

  category: CommandCategory

  icon: LucideIcon

  path?: string

  shortcut?: string
}
