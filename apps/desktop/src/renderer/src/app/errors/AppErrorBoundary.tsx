import { AlertTriangle, Home, RotateCcw } from 'lucide-react'
import { useNavigate, useRouteError } from 'react-router-dom'

export function AppErrorBoundary() {
  const error = useRouteError()
  const navigate = useNavigate()

  console.error(error)

  return (
    <div className="flex min-h-screen items-center justify-center bg-zinc-100 px-6">
      <div className="w-full max-w-lg rounded-2xl border border-zinc-200 bg-white p-8 shadow-xl">
        <div className="mb-6 flex items-center gap-3">
          <div className="rounded-xl bg-red-100 p-3">
            <AlertTriangle size={28} className="text-red-600" />
          </div>

          <div>
            <h1 className="text-2xl font-bold text-zinc-900">Something went wrong</h1>

            <p className="text-sm text-zinc-500">
              An unexpected error occurred while rendering this page.
            </p>
          </div>
        </div>

        <div className="rounded-xl bg-zinc-50 p-4">
          <p className="font-medium text-zinc-700">Error Details</p>

          <pre className="mt-2 overflow-auto text-xs text-red-600">
            {error instanceof Error ? error.message : 'Unknown error'}
          </pre>
        </div>

        <div className="mt-8 flex gap-3">
          <button
            onClick={() => window.location.reload()}
            className="flex flex-1 items-center justify-center gap-2 rounded-xl bg-zinc-900 px-4 py-3 text-white transition hover:bg-zinc-800"
          >
            <RotateCcw size={18} />
            Reload
          </button>

          <button
            onClick={() => navigate('/')}
            className="flex flex-1 items-center justify-center gap-2 rounded-xl border border-zinc-300 px-4 py-3 transition hover:bg-zinc-100"
          >
            <Home size={18} />
            Dashboard
          </button>
        </div>
      </div>
    </div>
  )
}
