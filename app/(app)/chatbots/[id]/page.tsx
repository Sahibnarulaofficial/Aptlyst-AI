'use client';

import { useState, use } from 'react';
import { mockChatbots } from '@/lib/mock-data';
import Link from 'next/link';
import { ArrowLeft, Bot, Copy, Check, ExternalLink, Activity, Users, MessageSquare } from 'lucide-react';
import { cn } from '@/lib/utils';
import { notFound } from 'next/navigation';

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'knowledge', label: 'Knowledge' },
  { id: 'conversations', label: 'Conversations' },
  { id: 'leads', label: 'Leads' },
  { id: 'appearance', label: 'Appearance' },
  { id: 'integrate', label: 'Integrate' },
];

export default function ChatbotDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const resolvedParams = use(params);
  const [activeTab, setActiveTab] = useState('overview');
  const [copied, setCopied] = useState(false);

  // If it's the mock ID from our creation form, generate a fake bot.
  // Otherwise try to find it in mock data.
  const isMockNew = resolvedParams.id.startsWith('bot_new_');
  const bot = isMockNew 
    ? {
        id: resolvedParams.id,
        name: 'Newly Created Bot',
        description: 'A freshly created chatbot instance.',
        status: 'draft' as const,
        conversationsCount: 0,
        leadsCount: 0,
        lastActive: new Date().toISOString(),
        primaryColor: '#3b82f6',
        websiteUrl: 'https://example.com'
      }
    : mockChatbots.find(b => b.id === resolvedParams.id);

  if (!bot) {
    notFound();
  }

  const embedSnippet = `<script
  src="https://app.aptlyst.ai/widget.js"
  data-bot-id="${bot.id}">
</script>`;

  const handleCopy = () => {
    navigator.clipboard.writeText(embedSnippet);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Header */}
      <div className="shrink-0 border-b border-slate-800 bg-slate-950/50 p-6 sm:p-8">
        <div className="max-w-7xl mx-auto space-y-6">
          <Link 
            href="/chatbots"
            className="inline-flex items-center gap-2 text-sm text-slate-400 hover:text-slate-100 transition-colors"
          >
            <ArrowLeft className="h-4 w-4" />
            Back to Chatbots
          </Link>
          
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
            <div className="flex items-center gap-4">
              <div 
                className="h-12 w-12 rounded-lg flex items-center justify-center shrink-0"
                style={{ backgroundColor: bot.primaryColor || '#1e293b' }}
              >
                <Bot className="h-6 w-6 text-white" />
              </div>
              <div>
                <h1 className="text-2xl font-bold tracking-tight text-slate-100 flex items-center gap-3">
                  {bot.name}
                  <span className={cn(
                    "inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium border",
                    bot.status === 'active' 
                      ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/20"
                      : "bg-slate-800 text-slate-400 border-slate-700"
                  )}>
                    {bot.status.charAt(0).toUpperCase() + bot.status.slice(1)}
                  </span>
                </h1>
                <p className="text-slate-400 text-sm mt-1">{bot.description}</p>
              </div>
            </div>
            
            <div className="flex gap-3">
              {bot.websiteUrl && (
                <a 
                  href={bot.websiteUrl}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-2 bg-slate-900 border border-slate-800 hover:bg-slate-800 text-slate-200 px-4 py-2 rounded-lg font-medium transition-colors text-sm"
                >
                  <ExternalLink className="h-4 w-4" />
                  View Site
                </a>
              )}
            </div>
          </div>

          {/* Tabs */}
          <div className="flex gap-6 overflow-x-auto border-b border-transparent">
            {TABS.map(tab => (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={cn(
                  "pb-3 text-sm font-medium transition-colors relative whitespace-nowrap",
                  activeTab === tab.id 
                    ? "text-blue-400" 
                    : "text-slate-400 hover:text-slate-200"
                )}
              >
                {tab.label}
                {activeTab === tab.id && (
                  <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-blue-500 rounded-t-full" />
                )}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto p-6 sm:p-8">
        <div className="max-w-7xl mx-auto">
          
          {activeTab === 'overview' && (
            <div className="space-y-6">
              <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
                <div className="bg-slate-950 border border-slate-800 rounded-xl p-6">
                  <div className="flex items-center gap-3 text-slate-400 mb-2">
                    <MessageSquare className="h-5 w-5" />
                    <h3 className="text-sm font-medium">Conversations</h3>
                  </div>
                  <p className="text-3xl font-bold text-slate-100">{bot.conversationsCount}</p>
                </div>
                <div className="bg-slate-950 border border-slate-800 rounded-xl p-6">
                  <div className="flex items-center gap-3 text-slate-400 mb-2">
                    <Users className="h-5 w-5" />
                    <h3 className="text-sm font-medium">Leads Captured</h3>
                  </div>
                  <p className="text-3xl font-bold text-slate-100">{bot.leadsCount}</p>
                </div>
                <div className="bg-slate-950 border border-slate-800 rounded-xl p-6">
                  <div className="flex items-center gap-3 text-slate-400 mb-2">
                    <Activity className="h-5 w-5" />
                    <h3 className="text-sm font-medium">Last Active</h3>
                  </div>
                  <p className="text-xl font-medium text-slate-100 mt-2">
                    {new Date(bot.lastActive).toLocaleDateString()}
                  </p>
                </div>
              </div>
            </div>
          )}

          {activeTab === 'integrate' && (
            <div className="max-w-3xl space-y-6">
              <div>
                <h2 className="text-lg font-medium text-slate-100">Add to your website</h2>
                <p className="text-sm text-slate-400 mt-1">
                  Copy and paste this code snippet into the <code>&lt;head&gt;</code> or just before the closing <code>&lt;/body&gt;</code> tag of your website.
                </p>
              </div>

              <div className="bg-slate-950 border border-slate-800 rounded-xl overflow-hidden">
                <div className="flex items-center justify-between px-4 py-2 border-b border-slate-800 bg-slate-900/50">
                  <span className="text-xs font-mono text-slate-400">HTML</span>
                  <button 
                    onClick={handleCopy}
                    className="inline-flex items-center gap-1.5 text-xs font-medium text-slate-300 hover:text-white transition-colors p-1"
                  >
                    {copied ? <Check className="h-3.5 w-3.5 text-emerald-400" /> : <Copy className="h-3.5 w-3.5" />}
                    {copied ? 'Copied!' : 'Copy code'}
                  </button>
                </div>
                <div className="p-4 overflow-x-auto">
                  <pre className="text-sm font-mono text-slate-300">
                    <code>{embedSnippet}</code>
                  </pre>
                </div>
              </div>
            </div>
          )}

          {/* Placeholder for other tabs */}
          {['knowledge', 'conversations', 'leads', 'appearance'].includes(activeTab) && (
            <div className="flex flex-col items-center justify-center h-64 border border-dashed border-slate-800 rounded-xl bg-slate-950/30">
              <p className="text-slate-400 text-sm">
                The {TABS.find(t => t.id === activeTab)?.label} section will be implemented in a future milestone.
              </p>
            </div>
          )}

        </div>
      </div>
    </div>
  );
}
