import { Bot, FileText, History, Home, Mic, Settings } from 'lucide-react'

import { Command } from '../types'
import { commandCategories } from './categories'

export const commands: Command[] = [
  {
    id: 'dashboard',
    title: 'Dashboard',
    description: 'Go to dashboard',
    category: commandCategories.navigation,
    icon: Home,
    path: '/'
  },

  {
    id: 'transcripts',
    title: 'Transcripts',
    description: 'Browse meeting transcripts',
    category: commandCategories.navigation,
    icon: FileText,
    path: '/transcripts'
  },

  {
    id: 'history',
    title: 'History',
    description: 'Meeting history',
    category: commandCategories.navigation,
    icon: History,
    path: '/history'
  },

  {
    id: 'meeting',
    title: 'Live Meeting',
    description: 'Start or join a meeting',
    category: commandCategories.meeting,
    icon: Mic,
    path: '/meeting'
  },

  {
    id: 'assistant',
    title: 'AI Assistant',
    description: 'Open AI assistant',
    category: commandCategories.ai,
    icon: Bot,
    path: '/assistant'
  },

  {
    id: 'settings',
    title: 'Settings',
    description: 'Application settings',
    category: commandCategories.system,
    icon: Settings,
    path: '/settings'
  }
]
