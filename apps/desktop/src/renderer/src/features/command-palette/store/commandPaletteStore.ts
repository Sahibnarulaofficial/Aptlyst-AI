import { create } from 'zustand'

type CommandPaletteStore = {
  open: boolean

  openPalette: () => void

  closePalette: () => void

  togglePalette: () => void
}

export const useCommandPaletteStore = create<CommandPaletteStore>((set) => ({
  open: false,

  openPalette: () =>
    set({
      open: true
    }),

  closePalette: () =>
    set({
      open: false
    }),

  togglePalette: () =>
    set((state) => ({
      open: !state.open
    }))
}))
