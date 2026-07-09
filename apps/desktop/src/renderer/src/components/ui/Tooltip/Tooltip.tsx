import { ReactNode } from 'react'

type TooltipProps = {
  children: ReactNode
  content: string
  disabled?: boolean
}

export function Tooltip({ children, content, disabled }: TooltipProps) {
  if (disabled) {
    return <>{children}</>
  }
  return (
    <div className="group relative block w-full">
      {children}

      <div
        className="
          pointer-events-none
          absolute
          left-full
          top-1/2
          z-50
          ml-3
          -translate-y-1/2
          whitespace-nowrap
          rounded-lg
          bg-zinc-900
          px-3
          py-2
          text-sm
          text-white
          opacity-0
          shadow-xl
          transition-all
          duration-150
          group-hover:opacity-100
        "
      >
        {content}
      </div>
    </div>
  )
}
