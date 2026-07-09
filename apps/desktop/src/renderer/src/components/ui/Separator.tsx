import { cn } from '@renderer/lib/cn'

export function Separator({ className }: { className?: string }) {
  return <div className={cn('h-px w-full bg-zinc-200', className)} />
}
