import * as React from 'react'
import { cn } from '@renderer/lib/cn'

export function Badge({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        'inline-flex items-center rounded-full bg-zinc-900 px-2.5 py-0.5 text-xs font-medium text-white',
        className
      )}
      {...props}
    />
  )
}
