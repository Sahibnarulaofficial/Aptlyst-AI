import { AppShell } from '@renderer/components/layout/AppShell'
import { Header } from '@renderer/components/layout/Header'
import { Sidebar } from '@renderer/components/layout/Sidebar'
import { StatusBar } from '@renderer/components/layout/StatusBar'
import { AppRouter } from '@renderer/app/router/AppRouter'

function App() {
  return (
    <AppShell
      sidebar={<Sidebar />}
      header={<Header />}
      workspace={<AppRouter />}
      statusBar={<StatusBar />}
    />
  )
}

export default App
