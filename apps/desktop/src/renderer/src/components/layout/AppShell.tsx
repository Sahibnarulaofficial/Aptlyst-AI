export function AppShell({
  sidebar,
  header,
  workspace,
  statusBar
}: {
  sidebar: React.ReactNode
  header: React.ReactNode
  workspace: React.ReactNode
  statusBar: React.ReactNode
}) {
  return (
    <div className="grid h-screen grid-cols-[260px_1fr] grid-rows-[64px_1fr_32px] overflow-hidden bg-zinc-100">
      <aside className="row-span-3 border-r bg-zinc-950">{sidebar}</aside>

      <header className="border-b bg-white">{header}</header>

      <main className="overflow-auto">{workspace}</main>

      <footer className="border-t bg-white">{statusBar}</footer>
    </div>
  )
}
