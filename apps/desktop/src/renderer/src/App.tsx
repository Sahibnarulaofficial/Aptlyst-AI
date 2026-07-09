import { AppRouter } from '@renderer/app/router/AppRouter'

import { useCommandPalette } from '@renderer/features/command-palette/hooks/useCommandPalette'

function App() {
  useCommandPalette()

  return <AppRouter />
}

export default App
