import { createHashRouter } from 'react-router-dom'
import { RootLayout } from '@renderer/app/layouts/RootLayout'
import { DashboardPage } from '@renderer/features/dashboard/pages/DashboardPage'
import { MeetingPage } from '@renderer/features/meeting/pages/MeetingPage'
import { TranscriptsPage } from '@renderer/features/transcripts/pages/TranscriptsPage'
import { AssistantPage } from '@renderer/features/assistant/pages/AssistantPage'
import { HistoryPage } from '@renderer/features/history/pages/HistoryPage'
import { SettingsPage } from '@renderer/features/settings/pages/SettingsPage'

export const router = createHashRouter([
  {
    path: '/',
    element: <RootLayout />,
    children: [
      {
        index: true,
        element: <DashboardPage />
      },
      {
        path: 'meeting',
        element: <MeetingPage />
      },
      {
        path: 'transcripts',
        element: <TranscriptsPage />
      },
      {
        path: 'assistant',
        element: <AssistantPage />
      },
      {
        path: 'history',
        element: <HistoryPage />
      },
      {
        path: 'settings',
        element: <SettingsPage />
      }
    ]
  }
])
