import { RouterProvider } from 'react-router-dom'

import { router } from './index'

export function AppRouter() {
  return <RouterProvider router={router} />
}
