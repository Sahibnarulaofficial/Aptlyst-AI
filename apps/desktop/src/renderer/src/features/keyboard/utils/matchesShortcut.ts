import { KeyboardShortcut } from '../types'

export function matchesShortcut(event: KeyboardEvent, shortcut: KeyboardShortcut) {
  return (
    event.key.toLowerCase() === shortcut.key.toLowerCase() &&
    !!event.ctrlKey === !!shortcut.ctrl &&
    !!event.shiftKey === !!shortcut.shift &&
    !!event.altKey === !!shortcut.alt &&
    !!event.metaKey === !!shortcut.meta
  )
}
