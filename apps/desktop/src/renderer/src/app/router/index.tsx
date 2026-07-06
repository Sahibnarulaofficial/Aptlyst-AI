import { createBrowserRouter } from 'react-router-dom'

import { DashboardPage } from '@renderer/features/dashboard/pages/DashboardPage'

export const router = createBrowserRouter([
  {
    path: '/',
    element: <DashboardPage />
  }
])
