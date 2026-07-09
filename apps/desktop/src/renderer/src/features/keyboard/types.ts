export interface KeyboardShortcut {
  id: string

  description: string

  key: string

  ctrl?: boolean

  shift?: boolean

  alt?: boolean

  meta?: boolean

  preventDefault?: boolean

  action: () => void
}
