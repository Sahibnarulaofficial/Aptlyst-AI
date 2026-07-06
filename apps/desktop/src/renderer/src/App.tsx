import { Badge } from '@renderer/components/ui/Badge'
import { Card, CardContent, CardHeader, CardTitle } from '@renderer/components/ui/Card'
import { Input } from '@renderer/components/ui/Input'
import { Button } from '@renderer/components/ui/Button'
import { Separator } from '@renderer/components/ui/Separator'

function App() {
  return (
    <main className="flex min-h-screen items-center justify-center bg-zinc-100 p-6">
      <Card className="w-[420px]">
        <CardHeader>
          <Badge className="w-fit">Prototype</Badge>
          <CardTitle className="mt-3 text-xl">Aptlyst AI</CardTitle>
        </CardHeader>

        <Separator />

        <CardContent className="space-y-4 pt-6">
          <Input placeholder="Meeting title..." />

          <Button className="w-full">Start Meeting</Button>
        </CardContent>
      </Card>
    </main>
  )
}

export default App
