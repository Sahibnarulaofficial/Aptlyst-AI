import { CommandCategory } from '../types'

export const commandCategories = {
  navigation: {
    id: 'navigation',
    title: 'Navigation',
    order: 1
  },

  meeting: {
    id: 'meeting',
    title: 'Meeting',
    order: 2
  },

  ai: {
    id: 'ai',
    title: 'AI',
    order: 3
  },

  system: {
    id: 'system',
    title: 'System',
    order: 4
  }
} satisfies Record<string, CommandCategory>
