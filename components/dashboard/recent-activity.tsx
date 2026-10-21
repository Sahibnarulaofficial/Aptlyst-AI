import { MessageSquare } from 'lucide-react';

export function RecentActivity() {
  return (
    <div className="bg-slate-950 border border-slate-800 rounded-xl overflow-hidden flex flex-col">
      <div className="p-6 border-b border-slate-800">
        <h3 className="text-lg font-medium text-slate-100">Recent Conversations</h3>
      </div>
      <div className="flex-1 flex flex-col items-center justify-center p-8 text-center min-h-[220px]">
        <div className="h-10 w-10 rounded-full bg-slate-900 border border-slate-800 flex items-center justify-center mb-3 text-slate-500">
          <MessageSquare className="h-5 w-5" />
        </div>
        <p className="text-sm font-medium text-slate-300">No conversations yet</p>
        <p className="text-xs text-slate-500 mt-1 max-w-xs">
          When visitors interact with your chatbots, conversation history will appear here.
        </p>
      </div>
    </div>
  );
}
