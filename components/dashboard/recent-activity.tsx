import { mockConversations, mockChatbots } from '@/lib/mock-data';
import { MessageSquare } from 'lucide-react';

export function RecentActivity() {
  const recent = [...mockConversations].sort(
    (a, b) => new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime()
  ).slice(0, 5);

  return (
    <div className="bg-slate-950 border border-slate-800 rounded-xl overflow-hidden">
      <div className="p-6 border-b border-slate-800">
        <h3 className="text-lg font-medium text-slate-100">Recent Conversations</h3>
      </div>
      <div className="divide-y divide-slate-800">
        {recent.map((conv) => {
          const bot = mockChatbots.find((b) => b.id === conv.chatbotId);
          return (
            <div key={conv.id} className="p-6 flex items-start gap-4">
              <div className="mt-1 bg-slate-900 p-2 rounded-lg border border-slate-800">
                <MessageSquare className="h-4 w-4 text-blue-500" />
              </div>
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-slate-200">
                  User in <span className="text-blue-400">{bot?.name || 'Unknown Bot'}</span>
                </p>
                <p className="text-sm text-slate-400 mt-1 truncate">{conv.snippet}</p>
              </div>
              <div className="text-xs text-slate-500 whitespace-nowrap">
                {new Date(conv.createdAt).toLocaleDateString()}
              </div>
            </div>
          );
        })}
        {recent.length === 0 && (
          <div className="p-8 text-center text-slate-500 text-sm">
            No recent activity
          </div>
        )}
      </div>
    </div>
  );
}
