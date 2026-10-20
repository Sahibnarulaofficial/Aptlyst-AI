import { StatCard } from '@/components/dashboard/stat-card';
import { RecentActivity } from '@/components/dashboard/recent-activity';
import { getDashboardStats } from '@/lib/mock-data';
import { Bot, MessageSquare, Users, Plus } from 'lucide-react';
import Link from 'next/link';

export default function DashboardPage() {
  const stats = getDashboardStats();

  return (
    <div className="p-8 max-w-7xl mx-auto space-y-8">
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4">
        <div>
          <h1 className="text-3xl font-bold tracking-tight text-slate-100">Overview</h1>
          <p className="text-slate-400 mt-1">Here is what is happening with your chatbots today.</p>
        </div>
        <Link 
          href="/chatbots/new"
          className="inline-flex items-center gap-2 bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg font-medium transition-colors text-sm"
        >
          <Plus className="h-4 w-4" />
          Create Chatbot
        </Link>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        <StatCard
          title="Total Chatbots"
          value={stats.totalChatbots}
          icon={<Bot className="h-5 w-5" />}
          description="Active and drafted bots"
        />
        <StatCard
          title="Total Conversations"
          value={stats.totalConversations}
          icon={<MessageSquare className="h-5 w-5" />}
          description="Across all your chatbots"
        />
        <StatCard
          title="Total Leads"
          value={stats.totalLeads}
          icon={<Users className="h-5 w-5" />}
          description="Captured contact details"
        />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <RecentActivity />
        <div className="bg-slate-950 border border-slate-800 rounded-xl p-6 flex flex-col justify-center items-center text-center h-full min-h-[300px]">
          <div className="h-12 w-12 rounded-full bg-slate-900 border border-slate-800 flex items-center justify-center mb-4">
            <Bot className="h-6 w-6 text-slate-400" />
          </div>
          <h3 className="text-lg font-medium text-slate-200 mb-2">Ready for more?</h3>
          <p className="text-sm text-slate-400 mb-6 max-w-sm">
            Create a new specialized AI agent to handle support, sales, or internal queries.
          </p>
          <Link 
            href="/chatbots/new"
            className="inline-flex items-center gap-2 bg-slate-100 hover:bg-white text-slate-900 px-4 py-2 rounded-lg font-medium transition-colors text-sm"
          >
            Create another chatbot
          </Link>
        </div>
      </div>
    </div>
  );
}
