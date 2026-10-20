import { mockChatbots } from '@/lib/mock-data';
import Link from 'next/link';
import { Plus, Search, Bot, ArrowRight } from 'lucide-react';
import { cn } from '@/lib/utils';

export default function ChatbotsPage() {
  return (
    <div className="p-8 max-w-7xl mx-auto space-y-8">
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4">
        <div>
          <h1 className="text-3xl font-bold tracking-tight text-slate-100">Chatbots</h1>
          <p className="text-slate-400 mt-1">Manage and configure your AI agents.</p>
        </div>
        <Link 
          href="/chatbots/new"
          className="inline-flex items-center gap-2 bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg font-medium transition-colors text-sm"
        >
          <Plus className="h-4 w-4" />
          Create Chatbot
        </Link>
      </div>

      <div className="bg-slate-950 border border-slate-800 rounded-xl overflow-hidden">
        <div className="p-4 border-b border-slate-800 flex items-center gap-4">
          <div className="relative flex-1 max-w-md">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-slate-500" />
            <input 
              type="text" 
              placeholder="Search chatbots..." 
              className="w-full bg-slate-900 border border-slate-800 rounded-lg pl-9 pr-4 py-2 text-sm text-slate-200 placeholder:text-slate-500 focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-all"
            />
          </div>
        </div>
        
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm whitespace-nowrap">
            <thead className="bg-slate-900/50 text-slate-400 border-b border-slate-800">
              <tr>
                <th className="px-6 py-4 font-medium">Name</th>
                <th className="px-6 py-4 font-medium">Status</th>
                <th className="px-6 py-4 font-medium">Conversations</th>
                <th className="px-6 py-4 font-medium">Leads</th>
                <th className="px-6 py-4 font-medium">Last Active</th>
                <th className="px-6 py-4 font-medium text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {mockChatbots.map((bot) => (
                <tr key={bot.id} className="hover:bg-slate-900/50 transition-colors">
                  <td className="px-6 py-4">
                    <div className="flex items-center gap-3">
                      <div 
                        className="h-8 w-8 rounded-md flex items-center justify-center shrink-0"
                        style={{ backgroundColor: bot.primaryColor || '#1e293b' }}
                      >
                        <Bot className="h-4 w-4 text-white" />
                      </div>
                      <div>
                        <div className="font-medium text-slate-200">{bot.name}</div>
                        <div className="text-xs text-slate-500 truncate max-w-[200px]">{bot.description}</div>
                      </div>
                    </div>
                  </td>
                  <td className="px-6 py-4">
                    <span className={cn(
                      "inline-flex items-center px-2 py-1 rounded-full text-xs font-medium border",
                      bot.status === 'active' 
                        ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/20"
                        : "bg-slate-800 text-slate-400 border-slate-700"
                    )}>
                      {bot.status.charAt(0).toUpperCase() + bot.status.slice(1)}
                    </span>
                  </td>
                  <td className="px-6 py-4 text-slate-300">
                    {bot.conversationsCount.toLocaleString()}
                  </td>
                  <td className="px-6 py-4 text-slate-300">
                    {bot.leadsCount.toLocaleString()}
                  </td>
                  <td className="px-6 py-4 text-slate-500 text-xs">
                    {new Date(bot.lastActive).toLocaleDateString()}
                  </td>
                  <td className="px-6 py-4 text-right">
                    <Link 
                      href={`/chatbots/${bot.id}`}
                      className="inline-flex items-center justify-center h-8 w-8 rounded-md text-slate-400 hover:text-slate-100 hover:bg-slate-800 transition-colors"
                    >
                      <ArrowRight className="h-4 w-4" />
                      <span className="sr-only">Manage {bot.name}</span>
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
