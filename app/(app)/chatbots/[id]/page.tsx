'use client';

import { useState, useEffect, use, useCallback } from 'react';
import Link from 'next/link';
import {
  ArrowLeft,
  Bot,
  Copy,
  Check,
  ExternalLink,
  Activity,
  Users,
  MessageSquare,
  Loader2,
  AlertCircle,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { useOrganization } from '@/lib/organization-context';
import { getChatbot, ApiError } from '@/lib/api';
import type { Chatbot } from '@/lib/types';

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
  const botId = resolvedParams.id;
  const { currentOrg } = useOrganization();

  const [activeTab, setActiveTab] = useState('overview');
  const [copied, setCopied] = useState(false);
  const [bot, setBot] = useState<Chatbot | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [isNotFound, setIsNotFound] = useState<boolean>(false);

  // Adjust state during render when organization or botId changes (React idiomatic pattern)
  const currentKey = `${currentOrg?.id}:${botId}`;
  const [prevKey, setPrevKey] = useState(currentKey);
  if (prevKey !== currentKey) {
    setPrevKey(currentKey);
    setBot(null);
    setError(null);
    setIsNotFound(false);
    setIsLoading(Boolean(currentOrg?.id && botId));
  }

  const fetchBot = useCallback(
    async (orgId: string, id: string) => {
      try {
        setIsLoading(true);
        setError(null);
        setIsNotFound(false);
        const data = await getChatbot(orgId, id);
        setBot(data);
      } catch (err: unknown) {
        if (err instanceof ApiError && err.status === 404) {
          setIsNotFound(true);
        } else {
          const message =
            err instanceof ApiError
              ? err.message
              : err instanceof Error
                ? err.message
                : 'Failed to load chatbot';
          setError(message);
        }
      } finally {
        setIsLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    if (!currentOrg?.id || !botId) {
      return;
    }
    const orgId = currentOrg.id;
    let ignore = false;

    async function load(targetOrgId: string, targetBotId: string) {
      try {
        const data = await getChatbot(targetOrgId, targetBotId);
        if (!ignore) {
          setBot(data);
        }
      } catch (err: unknown) {
        if (!ignore) {
          if (err instanceof ApiError && err.status === 404) {
            setIsNotFound(true);
          } else {
            const message =
              err instanceof ApiError
                ? err.message
                : err instanceof Error
                  ? err.message
                  : 'Failed to load chatbot';
            setError(message);
          }
        }
      } finally {
        if (!ignore) {
          setIsLoading(false);
        }
      }
    }

    load(orgId, botId);

    return () => {
      ignore = true;
    };
  }, [currentOrg?.id, botId]);

  const handleRetry = () => {
    if (currentOrg?.id && botId) {
      fetchBot(currentOrg.id, botId);
    }
  };

  if (isLoading) {
    return (
      <div className="p-8 max-w-7xl mx-auto space-y-6">
        <Link
          href="/chatbots"
          className="inline-flex items-center gap-2 text-sm text-slate-400 hover:text-slate-100 transition-colors"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to Chatbots
        </Link>
        <div className="p-16 text-center text-slate-400">
          <div className="inline-flex items-center gap-2">
            <Loader2 className="h-5 w-5 animate-spin text-blue-500" />
            <span className="text-sm">Loading chatbot details...</span>
          </div>
        </div>
      </div>
    );
  }

  if (isNotFound) {
    return (
      <div className="p-8 max-w-7xl mx-auto space-y-6">
        <Link
          href="/chatbots"
          className="inline-flex items-center gap-2 text-sm text-slate-400 hover:text-slate-100 transition-colors"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to Chatbots
        </Link>
        <div className="p-16 text-center space-y-4 bg-slate-950 border border-slate-800 rounded-xl">
          <div className="mx-auto h-12 w-12 rounded-xl bg-slate-900 border border-slate-800 flex items-center justify-center text-slate-500">
            <Bot className="h-6 w-6" />
          </div>
          <h2 className="text-lg font-medium text-slate-200">Chatbot not found</h2>
          <p className="text-sm text-slate-400 max-w-md mx-auto">
            This chatbot does not exist or does not belong to the selected organization.
          </p>
          <div>
            <Link
              href="/chatbots"
              className="inline-flex items-center gap-2 bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg font-medium transition-colors text-sm"
            >
              Return to Chatbots
            </Link>
          </div>
        </div>
      </div>
    );
  }

  if (error || !bot) {
    return (
      <div className="p-8 max-w-7xl mx-auto space-y-6">
        <Link
          href="/chatbots"
          className="inline-flex items-center gap-2 text-sm text-slate-400 hover:text-slate-100 transition-colors"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to Chatbots
        </Link>
        <div className="p-12 text-center space-y-4 bg-slate-950 border border-slate-800 rounded-xl">
          <div className="inline-flex items-center gap-2 text-red-400">
            <AlertCircle className="h-5 w-5" />
            <p className="text-sm font-medium">{error || 'Unable to display chatbot'}</p>
          </div>
          <div>
            <button
              type="button"
              onClick={handleRetry}
              className="px-4 py-2 bg-slate-900 border border-slate-800 hover:bg-slate-800 text-slate-200 text-sm font-medium rounded-lg transition-colors"
            >
              Retry
            </button>
          </div>
        </div>
      </div>
    );
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
                style={{ backgroundColor: bot.primary_color || '#1e293b' }}
              >
                <Bot className="h-6 w-6 text-white" />
              </div>
              <div>
                <h1 className="text-2xl font-bold tracking-tight text-slate-100 flex items-center gap-3">
                  {bot.name}
                  <span
                    className={cn(
                      'inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium border',
                      bot.status === 'active'
                        ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20'
                        : 'bg-slate-800 text-slate-400 border-slate-700',
                    )}
                  >
                    {bot.status.charAt(0).toUpperCase() + bot.status.slice(1)}
                  </span>
                </h1>
                <p className="text-slate-400 text-sm mt-1">{bot.description || 'No description provided'}</p>
              </div>
            </div>

            <div className="flex gap-3">
              {bot.website_url && (
                <a
                  href={bot.website_url}
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
            {TABS.map((tab) => (
              <button
                key={tab.id}
                type="button"
                onClick={() => setActiveTab(tab.id)}
                className={cn(
                  'pb-3 text-sm font-medium transition-colors relative whitespace-nowrap',
                  activeTab === tab.id ? 'text-blue-400' : 'text-slate-400 hover:text-slate-200',
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
                  <p className="text-2xl font-bold text-slate-500">—</p>
                  <p className="text-xs text-slate-500 mt-1">Analytics not yet available</p>
                </div>
                <div className="bg-slate-950 border border-slate-800 rounded-xl p-6">
                  <div className="flex items-center gap-3 text-slate-400 mb-2">
                    <Users className="h-5 w-5" />
                    <h3 className="text-sm font-medium">Leads Captured</h3>
                  </div>
                  <p className="text-2xl font-bold text-slate-500">—</p>
                  <p className="text-xs text-slate-500 mt-1">Lead tracking not yet available</p>
                </div>
                <div className="bg-slate-950 border border-slate-800 rounded-xl p-6">
                  <div className="flex items-center gap-3 text-slate-400 mb-2">
                    <Activity className="h-5 w-5" />
                    <h3 className="text-sm font-medium">Last Active</h3>
                  </div>
                  <p className="text-xl font-medium text-slate-500 mt-2">—</p>
                  <p className="text-xs text-slate-500 mt-1">Activity tracking not yet available</p>
                </div>
              </div>
            </div>
          )}

          {activeTab === 'integrate' && (
            <div className="max-w-3xl space-y-6">
              <div>
                <h2 className="text-lg font-medium text-slate-100">Add to your website</h2>
                <p className="text-sm text-slate-400 mt-1">
                  Copy and paste this code snippet into the <code>&lt;head&gt;</code> or just before the closing{' '}
                  <code>&lt;/body&gt;</code> tag of your website.
                </p>
              </div>

              <div className="bg-slate-950 border border-slate-800 rounded-xl overflow-hidden">
                <div className="flex items-center justify-between px-4 py-2 border-b border-slate-800 bg-slate-900/50">
                  <span className="text-xs font-mono text-slate-400">HTML</span>
                  <button
                    type="button"
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
                The {TABS.find((t) => t.id === activeTab)?.label} section will be implemented in a future milestone.
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
