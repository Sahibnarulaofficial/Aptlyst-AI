'use client';

import { useState, useEffect, use, useCallback } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
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
  Save,
  RotateCcw,
  CheckCircle2,
  Info,
  Settings as SettingsIcon,
  Sparkles,
  Sliders,
  Trash2,
  X,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { useOrganization } from '@/lib/organization-context';
import { getChatbot, updateChatbot, deleteChatbot, ApiError } from '@/lib/api';
import type { Chatbot, ChatbotStatus, ChatbotUpdateInput } from '@/lib/types';

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'settings', label: 'Settings' },
  { id: 'knowledge', label: 'Knowledge' },
  { id: 'conversations', label: 'Conversations' },
  { id: 'leads', label: 'Leads' },
  { id: 'integrate', label: 'Integrate' },
];

export default function ChatbotDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const resolvedParams = use(params);
  const botId = resolvedParams.id;
  const { currentOrg } = useOrganization();
  const router = useRouter();

  const [activeTab, setActiveTab] = useState('overview');
  const [copied, setCopied] = useState(false);
  const [bot, setBot] = useState<Chatbot | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [isNotFound, setIsNotFound] = useState<boolean>(false);

  // Form state for editing
  const [formData, setFormData] = useState({
    name: '',
    status: 'draft' as ChatbotStatus,
    description: '',
    websiteUrl: '',
    welcomeMessage: '',
    instructions: '',
    primaryColor: '#3b82f6',
  });
  const [isSaving, setIsSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveSuccess, setSaveSuccess] = useState(false);

  // Delete modal state
  const [isDeleteDialogOpen, setIsDeleteDialogOpen] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  // Adjust state during render when organization or botId changes (React idiomatic pattern)
  const currentKey = `${currentOrg?.id}:${botId}`;
  const [prevKey, setPrevKey] = useState(currentKey);
  if (prevKey !== currentKey) {
    setPrevKey(currentKey);
    setBot(null);
    setError(null);
    setIsNotFound(false);
    setSaveError(null);
    setSaveSuccess(false);
    setIsDeleteDialogOpen(false);
    setIsDeleting(false);
    setDeleteError(null);
    setIsLoading(Boolean(currentOrg?.id && botId));
  }

  const applyBotData = useCallback((data: Chatbot) => {
    setBot(data);
    setFormData({
      name: data.name,
      status: data.status,
      description: data.description || '',
      websiteUrl: data.website_url || '',
      welcomeMessage: data.welcome_message || '',
      instructions: data.instructions || '',
      primaryColor: data.primary_color || '#3b82f6',
    });
    setSaveError(null);
    setSaveSuccess(false);
  }, []);

  const fetchBot = useCallback(
    async (orgId: string, id: string) => {
      try {
        setIsLoading(true);
        setError(null);
        setIsNotFound(false);
        const data = await getChatbot(orgId, id);
        applyBotData(data);
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
    [applyBotData],
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
          applyBotData(data);
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
  }, [currentOrg?.id, botId, applyBotData]);

  const handleRetry = () => {
    if (currentOrg?.id && botId) {
      fetchBot(currentOrg.id, botId);
    }
  };

  const isDirty = Boolean(
    bot &&
      (formData.name !== bot.name ||
        formData.status !== bot.status ||
        formData.description !== (bot.description || '') ||
        formData.websiteUrl !== (bot.website_url || '') ||
        formData.welcomeMessage !== (bot.welcome_message || '') ||
        formData.instructions !== (bot.instructions || '') ||
        formData.primaryColor !== (bot.primary_color || '#3b82f6')),
  );

  const handleResetForm = () => {
    if (!bot) return;
    setFormData({
      name: bot.name,
      status: bot.status,
      description: bot.description || '',
      websiteUrl: bot.website_url || '',
      welcomeMessage: bot.welcome_message || '',
      instructions: bot.instructions || '',
      primaryColor: bot.primary_color || '#3b82f6',
    });
    setSaveError(null);
    setSaveSuccess(false);
  };

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    if (isSaving || !bot || !currentOrg?.id) return;

    const trimmedName = formData.name.trim();
    if (!trimmedName) {
      setSaveError('Chatbot name is required.');
      setSaveSuccess(false);
      return;
    }

    const trimmedUrl = formData.websiteUrl.trim();
    if (trimmedUrl && !trimmedUrl.startsWith('http://') && !trimmedUrl.startsWith('https://')) {
      setSaveError('Website URL must start with http:// or https://');
      setSaveSuccess(false);
      return;
    }

    setIsSaving(true);
    setSaveError(null);
    setSaveSuccess(false);

    const payload: ChatbotUpdateInput = {
      name: trimmedName,
      status: formData.status,
      description: formData.description.trim() || null,
      website_url: trimmedUrl || null,
      welcome_message: formData.welcomeMessage.trim() || null,
      instructions: formData.instructions.trim() || null,
      primary_color: formData.primaryColor || '#3b82f6',
    };

    try {
      const updated = await updateChatbot(currentOrg.id, bot.id, payload);
      applyBotData(updated);
      setSaveSuccess(true);
    } catch (err: unknown) {
      const message =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : 'Failed to update chatbot';
      setSaveError(message);
      setSaveSuccess(false);
    } finally {
      setIsSaving(false);
    }
  };

  const handleDelete = async () => {
    if (isDeleting || !bot || !currentOrg?.id) return;

    const targetOrgId = currentOrg.id;
    const targetBotId = bot.id;
    const deletedName = bot.name;

    setIsDeleting(true);
    setDeleteError(null);

    try {
      await deleteChatbot(targetOrgId, targetBotId);
      if (typeof window !== 'undefined') {
        try {
          sessionStorage.setItem('aptlyst_deleted_bot', deletedName);
        } catch {
          // sessionStorage may fail in private browsing
        }
      }
      setIsDeleteDialogOpen(false);
      router.push('/chatbots');
    } catch (err: unknown) {
      const message =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : 'Failed to delete chatbot';
      setDeleteError(message);
    } finally {
      setIsDeleting(false);
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
                className="h-12 w-12 rounded-lg flex items-center justify-center shrink-0 transition-colors"
                style={{ backgroundColor: bot.primary_color || '#1e293b' }}
              >
                <Bot className="h-6 w-6 text-white" />
              </div>
              <div>
                <h1 className="text-2xl font-bold tracking-tight text-slate-100 flex items-center gap-3">
                  {bot.name}
                  <span
                    className={cn(
                      'inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium border capitalize',
                      bot.status === 'active'
                        ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20'
                        : bot.status === 'inactive'
                          ? 'bg-amber-500/10 text-amber-400 border-amber-500/20'
                          : 'bg-slate-800 text-slate-400 border-slate-700',
                    )}
                  >
                    {bot.status}
                  </span>
                </h1>
                <p className="text-slate-400 text-sm mt-1">{bot.description || 'No description provided'}</p>
              </div>
            </div>

            <div className="flex items-center gap-3">
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
              {activeTab !== 'settings' && (
                <button
                  type="button"
                  onClick={() => setActiveTab('settings')}
                  className="inline-flex items-center gap-2 bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-200 px-4 py-2 rounded-lg font-medium transition-colors text-sm"
                >
                  <SettingsIcon className="h-4 w-4" />
                  Edit Settings
                </button>
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
                {tab.id === 'settings' && isDirty && (
                  <span className="ml-1.5 inline-block h-2 w-2 rounded-full bg-amber-400" title="Unsaved changes" />
                )}
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
          {/* OVERVIEW TAB */}
          {activeTab === 'overview' && (
            <div className="space-y-6">
              {/* Status explanation alert */}
              {bot.status === 'active' ? (
                <div className="p-4 bg-emerald-950/20 border border-emerald-800/40 rounded-xl flex items-start gap-3">
                  <CheckCircle2 className="h-5 w-5 text-emerald-400 shrink-0 mt-0.5" />
                  <div className="text-sm">
                    <p className="font-medium text-emerald-300">Status: Active</p>
                    <p className="text-slate-400 text-xs mt-0.5 leading-relaxed">
                      Chatbot configuration is active and saved. Website deployment will be activated once the public widget service milestone is released.
                    </p>
                  </div>
                </div>
              ) : bot.status === 'inactive' ? (
                <div className="p-4 bg-amber-950/20 border border-amber-800/40 rounded-xl flex items-start gap-3">
                  <AlertCircle className="h-5 w-5 text-amber-400 shrink-0 mt-0.5" />
                  <div className="text-sm">
                    <p className="font-medium text-amber-300">Status: Inactive</p>
                    <p className="text-slate-400 text-xs mt-0.5 leading-relaxed">
                      This chatbot is disabled. You can re-activate it anytime in Settings.
                    </p>
                  </div>
                </div>
              ) : (
                <div className="p-4 bg-slate-900/60 border border-slate-800 rounded-xl flex items-start gap-3">
                  <Info className="h-5 w-5 text-slate-400 shrink-0 mt-0.5" />
                  <div className="text-sm">
                    <p className="font-medium text-slate-300">Status: Draft</p>
                    <p className="text-slate-400 text-xs mt-0.5 leading-relaxed">
                      Chatbot is in drafting mode. Configure prompts and instructions in Settings before activating.
                    </p>
                  </div>
                </div>
              )}

              {/* Stat cards */}
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

              {/* Saved Configuration Inspector */}
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                <div className="bg-slate-950 border border-slate-800 rounded-xl p-6 space-y-4">
                  <div className="flex items-center justify-between border-b border-slate-800 pb-3">
                    <div className="flex items-center gap-2 text-slate-200 font-medium">
                      <Sparkles className="h-4 w-4 text-blue-400" />
                      <span>Prompts & Instructions</span>
                    </div>
                    <button
                      type="button"
                      onClick={() => setActiveTab('settings')}
                      className="text-xs text-blue-400 hover:text-blue-300 transition-colors"
                    >
                      Edit
                    </button>
                  </div>

                  <div className="space-y-3 text-sm">
                    <div>
                      <span className="text-xs text-slate-500 font-medium uppercase tracking-wider block mb-1">
                        Welcome Message
                      </span>
                      <div className="p-3 bg-slate-900 border border-slate-800 rounded-lg text-slate-300 text-xs italic">
                        {bot.welcome_message ? `"${bot.welcome_message}"` : 'No welcome message configured'}
                      </div>
                    </div>

                    <div>
                      <span className="text-xs text-slate-500 font-medium uppercase tracking-wider block mb-1">
                        System Instructions
                      </span>
                      <div className="p-3 bg-slate-900 border border-slate-800 rounded-lg text-slate-300 text-xs font-mono whitespace-pre-wrap max-h-48 overflow-y-auto">
                        {bot.instructions || 'No instructions configured'}
                      </div>
                    </div>
                  </div>
                </div>

                <div className="bg-slate-950 border border-slate-800 rounded-xl p-6 space-y-4">
                  <div className="flex items-center justify-between border-b border-slate-800 pb-3">
                    <div className="flex items-center gap-2 text-slate-200 font-medium">
                      <Sliders className="h-4 w-4 text-blue-400" />
                      <span>Details & Appearance</span>
                    </div>
                    <button
                      type="button"
                      onClick={() => setActiveTab('settings')}
                      className="text-xs text-blue-400 hover:text-blue-300 transition-colors"
                    >
                      Edit
                    </button>
                  </div>

                  <div className="space-y-3 text-sm">
                    <div className="flex items-center justify-between py-1.5 border-b border-slate-900">
                      <span className="text-xs text-slate-400">Website URL</span>
                      <span className="text-xs text-slate-200 font-mono truncate max-w-[240px]">
                        {bot.website_url || '—'}
                      </span>
                    </div>
                    <div className="flex items-center justify-between py-1.5 border-b border-slate-900">
                      <span className="text-xs text-slate-400">Primary Color</span>
                      <div className="flex items-center gap-2">
                        <div
                          className="h-4 w-4 rounded-full border border-slate-700"
                          style={{ backgroundColor: bot.primary_color || '#3b82f6' }}
                        />
                        <span className="text-xs text-slate-200 font-mono">
                          {bot.primary_color || '#3b82f6'}
                        </span>
                      </div>
                    </div>
                    <div className="flex items-center justify-between py-1.5 border-b border-slate-900">
                      <span className="text-xs text-slate-400">Created</span>
                      <span className="text-xs text-slate-400">
                        {new Date(bot.created_at).toLocaleDateString()}
                      </span>
                    </div>
                    <div className="flex items-center justify-between py-1.5">
                      <span className="text-xs text-slate-400">Last Updated</span>
                      <span className="text-xs text-slate-400">
                        {new Date(bot.updated_at).toLocaleDateString()}
                      </span>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* SETTINGS TAB */}
          {activeTab === 'settings' && (
            <div className="max-w-4xl space-y-8">
              <form onSubmit={handleSave} className="space-y-6">
              <div className="flex items-center justify-between">
                <div>
                  <h2 className="text-lg font-medium text-slate-100 flex items-center gap-2">
                    <span>Chatbot Settings</span>
                    {isDirty && (
                      <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[11px] font-medium bg-amber-500/10 text-amber-400 border border-amber-500/20">
                        Unsaved changes
                      </span>
                    )}
                  </h2>
                  <p className="text-sm text-slate-400 mt-0.5">
                    Configure your chatbot identity, prompts, brand styling, and status.
                  </p>
                </div>
              </div>

              {saveSuccess && (
                <div className="p-4 bg-emerald-950/30 border border-emerald-800/50 rounded-xl flex items-center gap-3 text-emerald-300 text-sm">
                  <CheckCircle2 className="h-5 w-5 shrink-0 text-emerald-400" />
                  <span>Chatbot settings saved successfully.</span>
                </div>
              )}

              {saveError && (
                <div className="p-4 bg-red-950/40 border border-red-800/60 rounded-xl flex items-center gap-3 text-red-300 text-sm">
                  <AlertCircle className="h-5 w-5 shrink-0 text-red-400" />
                  <span>{saveError}</span>
                </div>
              )}

              <div className="bg-slate-950 border border-slate-800 rounded-xl p-6 sm:p-8 space-y-8">
                {/* Status & Lifecycle */}
                <div className="space-y-4 border-b border-slate-800 pb-8">
                  <h3 className="text-sm font-medium text-slate-200 uppercase tracking-wider">
                    Status & Visibility
                  </h3>

                  <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
                    <label
                      className={cn(
                        'flex flex-col gap-1 p-4 rounded-xl border cursor-pointer transition-all',
                        formData.status === 'draft'
                          ? 'bg-slate-900 border-blue-500/50 ring-1 ring-blue-500/20'
                          : 'bg-slate-950 border-slate-800 hover:border-slate-700',
                      )}
                    >
                      <div className="flex items-center justify-between">
                        <span className="text-sm font-semibold text-slate-200">Draft</span>
                        <input
                          type="radio"
                          name="status"
                          value="draft"
                          checked={formData.status === 'draft'}
                          onChange={() => {
                            setFormData((prev) => ({ ...prev, status: 'draft' }));
                            setSaveSuccess(false);
                          }}
                          className="h-4 w-4 text-blue-600 focus:ring-blue-500 border-slate-700 bg-slate-900"
                        />
                      </div>
                      <p className="text-xs text-slate-500 mt-1 leading-relaxed">
                        Internal testing only. Chatbot is being drafted.
                      </p>
                    </label>

                    <label
                      className={cn(
                        'flex flex-col gap-1 p-4 rounded-xl border cursor-pointer transition-all',
                        formData.status === 'active'
                          ? 'bg-slate-900 border-blue-500/50 ring-1 ring-blue-500/20'
                          : 'bg-slate-950 border-slate-800 hover:border-slate-700',
                      )}
                    >
                      <div className="flex items-center justify-between">
                        <span className="text-sm font-semibold text-emerald-400">Active</span>
                        <input
                          type="radio"
                          name="status"
                          value="active"
                          checked={formData.status === 'active'}
                          onChange={() => {
                            setFormData((prev) => ({ ...prev, status: 'active' }));
                            setSaveSuccess(false);
                          }}
                          className="h-4 w-4 text-blue-600 focus:ring-blue-500 border-slate-700 bg-slate-900"
                        />
                      </div>
                      <p className="text-xs text-slate-500 mt-1 leading-relaxed">
                        Configuration is active. Public website embed in preview.
                      </p>
                    </label>

                    <label
                      className={cn(
                        'flex flex-col gap-1 p-4 rounded-xl border cursor-pointer transition-all',
                        formData.status === 'inactive'
                          ? 'bg-slate-900 border-blue-500/50 ring-1 ring-blue-500/20'
                          : 'bg-slate-950 border-slate-800 hover:border-slate-700',
                      )}
                    >
                      <div className="flex items-center justify-between">
                        <span className="text-sm font-semibold text-amber-400">Inactive</span>
                        <input
                          type="radio"
                          name="status"
                          value="inactive"
                          checked={formData.status === 'inactive'}
                          onChange={() => {
                            setFormData((prev) => ({ ...prev, status: 'inactive' }));
                            setSaveSuccess(false);
                          }}
                          className="h-4 w-4 text-blue-600 focus:ring-blue-500 border-slate-700 bg-slate-900"
                        />
                      </div>
                      <p className="text-xs text-slate-500 mt-1 leading-relaxed">
                        Disabled. Will not process any visitor interactions.
                      </p>
                    </label>
                  </div>
                </div>

                {/* Basic Information */}
                <div className="space-y-4 border-b border-slate-800 pb-8">
                  <h3 className="text-sm font-medium text-slate-200 uppercase tracking-wider">
                    Basic Information
                  </h3>

                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-6">
                    <div className="space-y-2">
                      <label htmlFor="edit-name" className="text-sm font-medium text-slate-300">
                        Chatbot Name *
                      </label>
                      <input
                        id="edit-name"
                        required
                        maxLength={255}
                        value={formData.name}
                        onChange={(e) => {
                          setFormData((prev) => ({ ...prev, name: e.target.value }));
                          setSaveSuccess(false);
                          if (saveError) setSaveError(null);
                        }}
                        className="w-full bg-slate-900 border border-slate-700 rounded-lg px-4 py-2 text-sm text-slate-200 placeholder:text-slate-500 focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-all"
                      />
                    </div>

                    <div className="space-y-2">
                      <label htmlFor="edit-websiteUrl" className="text-sm font-medium text-slate-300">
                        Website URL
                      </label>
                      <input
                        id="edit-websiteUrl"
                        type="url"
                        maxLength={2048}
                        value={formData.websiteUrl}
                        onChange={(e) => {
                          setFormData((prev) => ({ ...prev, websiteUrl: e.target.value }));
                          setSaveSuccess(false);
                          if (saveError) setSaveError(null);
                        }}
                        placeholder="https://example.com"
                        className="w-full bg-slate-900 border border-slate-700 rounded-lg px-4 py-2 text-sm text-slate-200 placeholder:text-slate-500 focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-all"
                      />
                    </div>
                  </div>

                  <div className="space-y-2">
                    <label htmlFor="edit-description" className="text-sm font-medium text-slate-300">
                      Description
                    </label>
                    <input
                      id="edit-description"
                      maxLength={2000}
                      value={formData.description}
                      onChange={(e) => {
                        setFormData((prev) => ({ ...prev, description: e.target.value }));
                        setSaveSuccess(false);
                      }}
                      placeholder="What does this chatbot do?"
                      className="w-full bg-slate-900 border border-slate-700 rounded-lg px-4 py-2 text-sm text-slate-200 placeholder:text-slate-500 focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-all"
                    />
                  </div>
                </div>

                {/* Prompts & Instructions */}
                <div className="space-y-4 border-b border-slate-800 pb-8">
                  <h3 className="text-sm font-medium text-slate-200 uppercase tracking-wider">
                    Behavior & Prompts
                  </h3>

                  <div className="space-y-2">
                    <label htmlFor="edit-welcomeMessage" className="text-sm font-medium text-slate-300">
                      Welcome Message
                    </label>
                    <input
                      id="edit-welcomeMessage"
                      maxLength={1000}
                      value={formData.welcomeMessage}
                      onChange={(e) => {
                        setFormData((prev) => ({ ...prev, welcomeMessage: e.target.value }));
                        setSaveSuccess(false);
                      }}
                      placeholder="Hi there! How can I help you today?"
                      className="w-full bg-slate-900 border border-slate-700 rounded-lg px-4 py-2 text-sm text-slate-200 placeholder:text-slate-500 focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-all"
                    />
                  </div>

                  <div className="space-y-2">
                    <label htmlFor="edit-instructions" className="text-sm font-medium text-slate-300">
                      System Instructions
                    </label>
                    <textarea
                      id="edit-instructions"
                      rows={5}
                      maxLength={5000}
                      value={formData.instructions}
                      onChange={(e) => {
                        setFormData((prev) => ({ ...prev, instructions: e.target.value }));
                        setSaveSuccess(false);
                      }}
                      placeholder="Define the bot persona, instructions, tone, and constraints..."
                      className="w-full bg-slate-900 border border-slate-700 rounded-lg px-4 py-2 text-sm text-slate-200 placeholder:text-slate-500 focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-all resize-y"
                    />
                  </div>
                </div>

                {/* Appearance */}
                <div className="space-y-4">
                  <h3 className="text-sm font-medium text-slate-200 uppercase tracking-wider">
                    Appearance & Branding
                  </h3>

                  <div className="space-y-2">
                    <label htmlFor="edit-primaryColor" className="text-sm font-medium text-slate-300">
                      Primary Brand Color
                    </label>
                    <div className="flex items-center gap-3">
                      <input
                        id="edit-primaryColor"
                        type="color"
                        value={formData.primaryColor}
                        onChange={(e) => {
                          setFormData((prev) => ({ ...prev, primaryColor: e.target.value }));
                          setSaveSuccess(false);
                        }}
                        className="h-10 w-10 rounded cursor-pointer border-0 p-0 bg-transparent"
                      />
                      <input
                        type="text"
                        maxLength={7}
                        value={formData.primaryColor}
                        onChange={(e) => {
                          setFormData((prev) => ({ ...prev, primaryColor: e.target.value }));
                          setSaveSuccess(false);
                        }}
                        className="bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-slate-200 font-mono w-28 focus:outline-none focus:border-blue-500"
                      />
                    </div>
                  </div>
                </div>
              </div>

              {/* Form Action Buttons */}
              <div className="flex items-center justify-end gap-3 pt-2">
                <button
                  type="button"
                  disabled={!isDirty || isSaving}
                  onClick={handleResetForm}
                  className="inline-flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium text-slate-400 hover:text-slate-200 hover:bg-slate-900 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  <RotateCcw className="h-4 w-4" />
                  Discard Changes
                </button>

                <button
                  type="submit"
                  disabled={!isDirty || isSaving || !formData.name.trim()}
                  className="inline-flex items-center gap-2 bg-blue-600 hover:bg-blue-700 text-white px-5 py-2 rounded-lg font-medium transition-colors text-sm disabled:opacity-50 disabled:cursor-not-allowed shadow-sm"
                >
                  {isSaving ? (
                    <>
                      <Loader2 className="h-4 w-4 animate-spin" />
                      Saving…
                    </>
                  ) : (
                    <>
                      <Save className="h-4 w-4" />
                      Save Changes
                    </>
                  )}
                </button>
              </div>
            </form>

            {/* Danger Zone */}
            <div className="pt-6 border-t border-slate-800">
              <div className="border border-red-900/40 bg-red-950/10 rounded-xl p-6 sm:p-8 space-y-4">
                <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
                  <div className="space-y-1">
                    <h3 className="text-xs font-semibold uppercase tracking-wider text-red-400">
                      Danger Zone
                    </h3>
                    <p className="text-sm font-medium text-slate-200">Delete this chatbot</p>
                    <p className="text-xs text-slate-400 max-w-xl leading-relaxed">
                      Once deleted, this chatbot and all of its configuration will be permanently removed. This action cannot be undone.
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={() => {
                      setDeleteError(null);
                      setIsDeleteDialogOpen(true);
                    }}
                    className="inline-flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium text-red-400 hover:text-red-300 bg-red-950/40 hover:bg-red-900/40 border border-red-800/60 transition-colors shrink-0"
                  >
                    <Trash2 className="h-4 w-4" />
                    <span>Delete Chatbot</span>
                  </button>
                </div>
              </div>
            </div>
          </div>
        )}

          {/* INTEGRATE TAB */}
          {activeTab === 'integrate' && (
            <div className="max-w-3xl space-y-6">
              <div>
                <h2 className="text-lg font-medium text-slate-100">Add to your website</h2>
                <p className="text-sm text-slate-400 mt-1">
                  Copy and paste this code snippet into the <code>&lt;head&gt;</code> or just before the closing{' '}
                  <code>&lt;/body&gt;</code> tag of your website.
                </p>
              </div>

              {/* Status semantics callout */}
              <div className="p-4 bg-blue-950/30 border border-blue-800/40 rounded-xl space-y-1 text-sm">
                <div className="flex items-center gap-2 font-medium text-blue-300">
                  <Info className="h-4 w-4 shrink-0 text-blue-400" />
                  <span>Widget Integration in Development</span>
                </div>
                <p className="text-xs text-slate-400 pl-6 leading-relaxed">
                  Public website embed is currently in preview. The script below will be activated when the widget hosting service is completed in an upcoming milestone. Your bot configuration and status will be preserved.
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
          {['knowledge', 'conversations', 'leads'].includes(activeTab) && (
            <div className="flex flex-col items-center justify-center h-64 border border-dashed border-slate-800 rounded-xl bg-slate-950/30">
              <p className="text-slate-400 text-sm">
                The {TABS.find((t) => t.id === activeTab)?.label} section will be implemented in a future milestone.
              </p>
            </div>
          )}
        </div>
      </div>

      {/* Delete Confirmation Dialog */}
      {isDeleteDialogOpen && (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="delete-dialog-title"
          className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm"
          onClick={(e) => {
            if (e.target === e.currentTarget && !isDeleting) {
              setIsDeleteDialogOpen(false);
              setDeleteError(null);
            }
          }}
          onKeyDown={(e) => {
            if (e.key === 'Escape' && !isDeleting) {
              setIsDeleteDialogOpen(false);
              setDeleteError(null);
            }
          }}
        >
          <div className="w-full max-w-md bg-slate-950 border border-slate-800 rounded-xl p-6 shadow-2xl space-y-5 animate-in fade-in-50 zoom-in-95 duration-150">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2.5">
                <div className="h-9 w-9 rounded-lg bg-red-950/50 border border-red-800/60 flex items-center justify-center text-red-400">
                  <Trash2 className="h-5 w-5" />
                </div>
                <div>
                  <h2 id="delete-dialog-title" className="text-base font-semibold text-slate-100">
                    Delete Chatbot
                  </h2>
                  <p className="text-xs text-slate-400">Permanently delete this agent</p>
                </div>
              </div>
              <button
                type="button"
                onClick={() => {
                  if (!isDeleting) {
                    setIsDeleteDialogOpen(false);
                    setDeleteError(null);
                  }
                }}
                disabled={isDeleting}
                className="text-slate-400 hover:text-slate-200 p-1.5 rounded-lg transition-colors disabled:opacity-50"
                aria-label="Close dialog"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            {deleteError && (
              <div
                role="alert"
                className="p-3 bg-red-950/40 border border-red-800/60 rounded-lg flex items-start gap-2.5 text-red-300 text-xs"
              >
                <AlertCircle className="h-4 w-4 shrink-0 text-red-400 mt-0.5" />
                <span>{deleteError}</span>
              </div>
            )}

            <div className="space-y-2 text-sm text-slate-300">
              <p>
                Are you sure you want to delete <span className="font-semibold text-slate-100">&ldquo;{bot?.name}&rdquo;</span>?
              </p>
              <p className="text-xs text-slate-400 leading-relaxed">
                This action is irreversible. All configuration, prompt instructions, branding, and status settings for this chatbot will be permanently removed.
              </p>
            </div>

            <div className="flex items-center justify-end gap-3 pt-2 border-t border-slate-800/80">
              <button
                type="button"
                onClick={() => {
                  setIsDeleteDialogOpen(false);
                  setDeleteError(null);
                }}
                disabled={isDeleting}
                className="px-4 py-2 rounded-lg text-sm font-medium text-slate-300 hover:text-white hover:bg-slate-900 transition-colors disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleDelete}
                disabled={isDeleting}
                className="inline-flex items-center gap-2 bg-red-600 hover:bg-red-700 text-white px-4 py-2 rounded-lg font-medium transition-colors text-sm disabled:opacity-50 disabled:cursor-not-allowed shadow-sm"
              >
                {isDeleting ? (
                  <>
                    <Loader2 className="h-4 w-4 animate-spin" />
                    <span>Deleting…</span>
                  </>
                ) : (
                  <>
                    <Trash2 className="h-4 w-4" />
                    <span>Delete Chatbot</span>
                  </>
                )}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
