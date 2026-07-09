import { useCommandPaletteStore } from '../store/commandPaletteStore'

import { useKeyboardShortcut } from '@renderer/features/keyboard/hooks/useKeyboardShortcut'

export function useCommandPalette() {
  const togglePalette = useCommandPaletteStore((state) => state.togglePalette)

  useKeyboardShortcut({
    id: 'command-palette',
    description: 'Open Command Palette',
    key: 'k',
    ctrl: true,
    preventDefault: true,
    action: togglePalette
  })
}
