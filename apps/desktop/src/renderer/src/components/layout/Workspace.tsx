import { MessageSquareText } from 'lucide-react'

export function Workspace() {
  return (
    <div className="flex h-full items-center justify-center bg-zinc-100 p-8">
      <div className="max-w-lg rounded-2xl border border-dashed border-zinc-300 bg-white p-10 text-center shadow-sm">
        <div className="mx-auto mb-6 flex h-16 w-16 items-center justify-center rounded-full bg-zinc-900 text-white">
          <MessageSquareText size={30} />
        </div>

        <h2 className="text-2xl font-semibold text-zinc-900">Ready for your first meeting</h2>

        <p className="mt-3 text-sm leading-6 text-zinc-500">
          Start a meeting to capture conversations, generate live AI suggestions, summaries, and
          action items.
        </p>
      </div>
    </div>
  )
}
