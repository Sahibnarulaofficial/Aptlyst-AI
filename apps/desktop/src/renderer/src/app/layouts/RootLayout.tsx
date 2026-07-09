import { Outlet } from 'react-router-dom'

import { AppShell } from '@renderer/components/layout/AppShell'
import { Header } from '@renderer/components/layout/Header'
import { Sidebar } from '@renderer/components/layout/Sidebar'
import { StatusBar } from '@renderer/components/layout/StatusBar'

import { CommandPalette } from '@renderer/features/command-palette/components/CommandPalette'
export function RootLayout() {
  return (
    <>
      <AppShell
        sidebar={<Sidebar />}
        header={<Header />}
        workspace={<Outlet />}
        statusBar={<StatusBar />}
      />
      <CommandPalette />
    </>
  )
}
