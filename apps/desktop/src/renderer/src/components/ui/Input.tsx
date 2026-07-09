import * as React from 'react'
import { cn } from '@renderer/lib/cn'

type InputProps = React.InputHTMLAttributes<HTMLInputElement>
export function Input({ className, ...props }: InputProps) {
  return (
    <input
      className={cn(
        'flex h-10 w-full rounded-lg border border-zinc-300 bg-white px-3 py-2 text-sm outline-none focus:border-zinc-900',
        className
      )}
      {...props}
    />
  )
}
