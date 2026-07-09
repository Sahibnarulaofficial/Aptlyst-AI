import { useEffect } from 'react'

import { KeyboardShortcut } from '../types'
import { matchesShortcut } from '../utils/matchesShortcut'

export function useKeyboardShortcut(shortcut: KeyboardShortcut) {
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (!matchesShortcut(event, shortcut)) {
        return
      }

      if (shortcut.preventDefault) {
        event.preventDefault()
      }

      shortcut.action()
    }

    window.addEventListener('keydown', onKeyDown)

    return () => window.removeEventListener('keydown', onKeyDown)
  }, [shortcut])
}
