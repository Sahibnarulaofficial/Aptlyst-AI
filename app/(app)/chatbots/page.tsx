'use client';

import { useState, useEffect, useCallback } from 'react';
import Link from 'next/link';
import { Plus, Search, Bot, ArrowRight, Loader2, AlertCircle, CheckCircle2, X } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useOrganization } from '@/lib/organization-context';
import { listChatbots, ApiError } from '@/lib/api';
import type { Chatbot } from '@/lib/types';

export default function ChatbotsPage() {
  const { currentOrg } = useOrganization();
  const [chatbots, setChatbots] = useState<Chatbot[]>([]);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState<string>('');
  const [deletedMessage, setDeletedMessage] = useState<string | null>(() => {
    if (typeof window !== 'undefined') {
      try {
        const deletedName = sessionStorage.getItem('aptlyst_deleted_bot');
        if (deletedName) {
          sessionStorage.removeItem('aptlyst_deleted_bot');
          return `Chatbot "${deletedName}" was successfully deleted.`;
        }
      } catch {
        // sessionStorage unavailable
      }
    }
    return null;
  });

  // Adjust state during render when organization changes (React idiomatic pattern)
  const [prevOrgId, setPrevOrgId] = useState(currentOrg?.id);
  if (prevOrgId !== currentOrg?.id) {
    setPrevOrgId(currentOrg?.id);
    setChatbots([]);
    setError(null);
    setDeletedMessage(null);
    setIsLoading(Boolean(currentOrg?.id));
  }

  const fetchBots = useCallback(async (orgId: string) => {
    try {
      setIsLoading(true);
      setError(null);
      const data = await listChatbots(orgId);
      setChatbots(data);
    } catch (err: unknown) {
      const message =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : 'Failed to load chatbots';
      setError(message);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!currentOrg?.id) {
      return;
    }
    const orgId = currentOrg.id;
    let ignore = false;

    async function load(targetOrgId: string) {
      try {
        const data = await listChatbots(targetOrgId);
        if (!ignore) {
          setChatbots(data);
        }
      } catch (err: unknown) {
        if (!ignore) {
          const message =
            err instanceof ApiError
              ? err.message
              : err instanceof Error
                ? err.message
                : 'Failed to load chatbots';
          setError(message);
        }
      } finally {
        if (!ignore) {
          setIsLoading(false);
        }
      }
    }

    load(orgId);

    return () => {
      ignore = true;
    };
  }, [currentOrg?.id]);

  const handleRetry = () => {
    if (currentOrg?.id) {
      fetchBots(currentOrg.id);
    }
  };

  const filteredChatbots = chatbots.filter(
    (bot) =>
      bot.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
      (bot.description && bot.description.toLowerCase().includes(searchQuery.toLowerCase())),
  );

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

      {deletedMessage && (
        <div className="p-4 bg-emerald-950/30 border border-emerald-800/50 rounded-xl flex items-center justify-between gap-3 text-emerald-300 text-sm">
          <div className="flex items-center gap-2.5">
            <CheckCircle2 className="h-5 w-5 shrink-0 text-emerald-400" />
            <span>{deletedMessage}</span>
          </div>
          <button
            type="button"
            onClick={() => setDeletedMessage(null)}
            className="text-emerald-400 hover:text-emerald-200 p-1 rounded-md transition-colors"
            aria-label="Dismiss message"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      )}

      <div className="bg-slate-950 border border-slate-800 rounded-xl overflow-hidden">
        <div className="p-4 border-b border-slate-800 flex items-center gap-4">
          <div className="relative flex-1 max-w-md">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-slate-500" />
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search chatbots..."
              className="w-full bg-slate-900 border border-slate-800 rounded-lg pl-9 pr-4 py-2 text-sm text-slate-200 placeholder:text-slate-500 focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-all"
            />
          </div>
        </div>

        {error ? (
          <div className="p-8 text-center space-y-4">
            <div className="inline-flex items-center gap-2 text-red-400">
              <AlertCircle className="h-5 w-5" />
              <p className="text-sm font-medium">{error}</p>
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
        ) : isLoading ? (
          <div className="p-16 text-center text-slate-400">
            <div className="inline-flex items-center gap-2">
              <Loader2 className="h-5 w-5 animate-spin text-blue-500" />
              <span className="text-sm">Loading chatbots...</span>
            </div>
          </div>
        ) : chatbots.length === 0 ? (
          <div className="p-16 text-center space-y-4">
            <div className="mx-auto h-12 w-12 rounded-xl bg-slate-900 border border-slate-800 flex items-center justify-center text-slate-500">
              <Bot className="h-6 w-6" />
            </div>
            <div>
              <h3 className="text-base font-medium text-slate-200">No chatbots yet</h3>
              <p className="text-sm text-slate-500 mt-1 max-w-sm mx-auto">
                Create your first AI chatbot to start engaging visitors and automating support.
              </p>
            </div>
            <Link
              href="/chatbots/new"
              className="inline-flex items-center gap-2 bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg font-medium transition-colors text-sm"
            >
              <Plus className="h-4 w-4" />
              Create Chatbot
            </Link>
          </div>
        ) : filteredChatbots.length === 0 ? (
          <div className="p-12 text-center text-slate-400 text-sm">
            No chatbots match your search &quot;{searchQuery}&quot;.
          </div>
        ) : (
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
                {filteredChatbots.map((bot) => (
                  <tr key={bot.id} className="hover:bg-slate-900/50 transition-colors">
                    <td className="px-6 py-4">
                      <div className="flex items-center gap-3">
                        <div
                          className="h-8 w-8 rounded-md flex items-center justify-center shrink-0"
                          style={{ backgroundColor: bot.primary_color || '#1e293b' }}
                        >
                          <Bot className="h-4 w-4 text-white" />
                        </div>
                        <div>
                          <div className="font-medium text-slate-200">{bot.name}</div>
                          <div className="text-xs text-slate-500 truncate max-w-[200px]">
                            {bot.description || '—'}
                          </div>
                        </div>
                      </div>
                    </td>
                    <td className="px-6 py-4">
                      <span
                        className={cn(
                          'inline-flex items-center px-2 py-1 rounded-full text-xs font-medium border',
                          bot.status === 'active'
                            ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20'
                            : 'bg-slate-800 text-slate-400 border-slate-700',
                        )}
                      >
                        {bot.status.charAt(0).toUpperCase() + bot.status.slice(1)}
                      </span>
                    </td>
                    <td className="px-6 py-4 text-slate-500" title="Analytics not yet available">
                      —
                    </td>
                    <td className="px-6 py-4 text-slate-500" title="Lead tracking not yet available">
                      —
                    </td>
                    <td className="px-6 py-4 text-slate-500 text-xs" title="Activity tracking not yet available">
                      —
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
        )}
      </div>
    </div>
  );
}
