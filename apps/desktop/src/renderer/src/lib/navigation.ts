import { Bot, History, Home, Mic, FileText, Settings } from 'lucide-react'

export const navigation = [
  {
    title: 'Dashboard',
    path: '/',
    icon: Home
  },
  {
    title: 'Live Meeting',
    path: '/meeting',
    icon: Mic
  },
  {
    title: 'Transcripts',
    path: '/transcripts',
    icon: FileText
  },
  {
    title: 'AI Assistant',
    path: '/assistant',
    icon: Bot
  },
  {
    title: 'History',
    path: '/history',
    icon: History
  },
  {
    title: 'Settings',
    path: '/settings',
    icon: Settings
  }
]
