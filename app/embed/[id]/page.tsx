'use client';

import { use, useEffect, useState } from 'react';
import { Bot, AlertCircle, Loader2, Send, Info } from 'lucide-react';
import { getPublicChatbot, ApiError } from '@/lib/api';
import type { PublicChatbotConfig } from '@/lib/types';

export default function PublicEmbedPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const resolvedParams = use(params);
  const botId = resolvedParams.id;

  const [bot, setBot] = useState<PublicChatbotConfig | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [isNotFound, setIsNotFound] = useState(false);

  useEffect(() => {
    let ignore = false;

    async function load() {
      if (!botId) return;

      setIsLoading(true);
      setError(null);
      setIsNotFound(false);

      try {
        const config = await getPublicChatbot(botId);
        if (!ignore) {
          setBot(config);
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
                  : 'Failed to load chatbot configuration';
            setError(message);
          }
        }
      } finally {
        if (!ignore) {
          setIsLoading(false);
        }
      }
    }

    load();

    return () => {
      ignore = true;
    };
  }, [botId]);

  // Loading State
  if (isLoading) {
    return (
      <div className="flex flex-col h-screen w-full bg-slate-950 text-slate-100 items-center justify-center p-6 select-none">
        <div className="flex flex-col items-center gap-3 text-slate-400">
          <Loader2 className="h-7 w-7 animate-spin text-blue-500" />
          <p className="text-sm font-medium">Loading chatbot…</p>
        </div>
      </div>
    );
  }

  // Not Found / Inactive State
  if (isNotFound) {
    return (
      <div className="flex flex-col h-screen w-full bg-slate-950 text-slate-100 items-center justify-center p-6 text-center">
        <div
          role="alert"
          className="max-w-xs p-6 bg-slate-900 border border-slate-800 rounded-2xl shadow-xl flex flex-col items-center gap-3"
        >
          <div className="h-10 w-10 rounded-full bg-amber-500/10 border border-amber-500/20 flex items-center justify-center text-amber-400">
            <AlertCircle className="h-5 w-5" />
          </div>
          <div>
            <h1 className="text-base font-semibold text-slate-100">
              Chatbot Unavailable
            </h1>
            <p className="text-xs text-slate-400 mt-1 leading-relaxed">
              This chatbot is either offline, in draft mode, or does not exist.
            </p>
          </div>
        </div>
      </div>
    );
  }

  // Generic Error State
  if (error || !bot) {
    return (
      <div className="flex flex-col h-screen w-full bg-slate-950 text-slate-100 items-center justify-center p-6 text-center">
        <div
          role="alert"
          className="max-w-xs p-6 bg-slate-900 border border-red-900/40 rounded-2xl shadow-xl flex flex-col items-center gap-3"
        >
          <div className="h-10 w-10 rounded-full bg-red-500/10 border border-red-500/20 flex items-center justify-center text-red-400">
            <AlertCircle className="h-5 w-5" />
          </div>
          <div>
            <h1 className="text-base font-semibold text-slate-100">
              Connection Error
            </h1>
            <p className="text-xs text-slate-400 mt-1 leading-relaxed">
              {error || 'Unable to load chatbot at this time.'}
            </p>
          </div>
        </div>
      </div>
    );
  }

  const primaryColor = bot.primary_color || '#3b82f6';
  const welcomeMessage =
    bot.welcome_message?.trim() || 'Hello! How can I help you today?';

  return (
    <div className="flex flex-col h-screen w-full bg-slate-950 text-slate-100 overflow-hidden font-sans">
      {/* Header */}
      <header className="shrink-0 flex items-center justify-between px-4 py-3 border-b border-slate-800/80 bg-slate-900/60 backdrop-blur-sm">
        <div className="flex items-center gap-3 min-w-0">
          <div
            className="h-9 w-9 rounded-full flex items-center justify-center shrink-0 shadow-sm transition-colors text-white"
            style={{ backgroundColor: primaryColor }}
          >
            <Bot className="h-5 w-5" />
          </div>
          <div className="min-w-0">
            <h1 className="text-sm font-semibold text-slate-100 truncate">
              {bot.name}
            </h1>
            <div className="flex items-center gap-1.5 mt-0.5">
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-pulse" />
              <span className="text-[11px] text-slate-400 font-medium">Online</span>
            </div>
          </div>
        </div>

        <span className="text-[10px] uppercase tracking-wider font-semibold px-2 py-0.5 rounded-full bg-blue-500/10 text-blue-400 border border-blue-500/20">
          Preview
        </span>
      </header>

      {/* Messages Stream */}
      <main className="flex-1 overflow-y-auto p-4 space-y-4">
        {/* Welcome Message Bubble */}
        <div className="flex items-start gap-2.5 max-w-[85%]">
          <div
            className="h-7 w-7 rounded-full flex items-center justify-center shrink-0 text-white text-xs mt-0.5 shadow-sm"
            style={{ backgroundColor: primaryColor }}
          >
            <Bot className="h-4 w-4" />
          </div>
          <div className="space-y-1">
            <div className="p-3.5 bg-slate-900 border border-slate-800 rounded-2xl rounded-tl-sm text-sm text-slate-200 leading-relaxed shadow-sm">
              <p className="whitespace-pre-wrap">{welcomeMessage}</p>
            </div>
          </div>
        </div>

        {/* Milestone Skeleton Notice */}
        <div className="my-3 p-3 bg-slate-900/50 border border-slate-800/60 rounded-xl flex items-start gap-2.5 text-xs text-slate-400">
          <Info className="h-4 w-4 shrink-0 text-blue-400 mt-0.5" />
          <p className="leading-relaxed">
            <span className="font-medium text-slate-300">Widget Preview:</span>{' '}
            Visual branding and welcome message loaded successfully. Live AI
            conversations will be enabled in an upcoming release.
          </p>
        </div>
      </main>

      {/* Input / Composer Area (Skeleton / Preview Mode) */}
      <footer className="shrink-0 p-3 border-t border-slate-800/80 bg-slate-900/40 space-y-2">
        <div className="flex items-center gap-2 bg-slate-900 border border-slate-800 rounded-xl px-3 py-2 text-sm text-slate-500 focus-within:border-slate-700">
          <input
            type="text"
            disabled
            placeholder="Live chat is disabled in preview mode…"
            className="flex-1 bg-transparent text-slate-400 placeholder-slate-600 outline-none text-xs cursor-not-allowed"
            aria-label="Message input disabled in preview mode"
          />
          <button
            type="button"
            disabled
            className="p-1.5 rounded-lg text-slate-600 bg-slate-800/50 cursor-not-allowed"
            aria-label="Send message disabled in preview mode"
          >
            <Send className="h-3.5 w-3.5" />
          </button>
        </div>
        <div className="text-center">
          <span className="text-[10px] text-slate-500 tracking-wide font-medium">
            Powered by Aptlyst AI
          </span>
        </div>
      </footer>
    </div>
  );
}
