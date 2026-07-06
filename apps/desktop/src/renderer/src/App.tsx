import { AppShell } from '@renderer/components/layout/AppShell'
import { Header } from '@renderer/components/layout/Header'
import { Sidebar } from '@renderer/components/layout/Sidebar'
import { StatusBar } from '@renderer/components/layout/StatusBar'
import { Workspace } from '@renderer/components/layout/Workspace'

function App() {
  return (
    <AppShell
      sidebar={<Sidebar />}
      header={<Header />}
      workspace={<Workspace />}
      statusBar={<StatusBar />}
    />
  )
}

export default App
