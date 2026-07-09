import { Bot, FileText, History, Home, Mic, Settings } from 'lucide-react'

export const navigation = [
  {
    id: 'dashboard',
    title: 'Dashboard',
    description: 'Welcome back',
    path: '/',
    icon: Home
  },
  {
    id: 'meeting',
    title: 'Live Meeting',
    description: 'Capture and assist meetings',
    path: '/meeting',
    icon: Mic
  },
  {
    id: 'transcripts',
    title: 'Transcripts',
    description: 'Browse saved transcripts',
    path: '/transcripts',
    icon: FileText
  },
  {
    id: 'assistant',
    title: 'AI Assistant',
    description: 'Chat with Aptlyst AI',
    path: '/assistant',
    icon: Bot
  },
  {
    id: 'history',
    title: 'History',
    description: 'Previous meetings',
    path: '/history',
    icon: History
  },
  {
    id: 'settings',
    title: 'Settings',
    description: 'Application preferences',
    path: '/settings',
    icon: Settings
  }
]
