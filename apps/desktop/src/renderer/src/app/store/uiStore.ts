import { create } from 'zustand'

type UIStore = {
  sidebarCollapsed: boolean
  toggleSidebar: () => void
  setSidebarCollapsed: (collapsed: boolean) => void
}

export const useUIStore = create<UIStore>((set) => ({
  sidebarCollapsed: false,

  toggleSidebar: () =>
    set((state) => ({
      sidebarCollapsed: !state.sidebarCollapsed
    })),

  setSidebarCollapsed: (collapsed) =>
    set({
      sidebarCollapsed: collapsed
    })
}))
