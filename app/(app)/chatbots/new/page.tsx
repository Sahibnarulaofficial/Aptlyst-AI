'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { ArrowLeft, Save, AlertCircle } from 'lucide-react';
import Link from 'next/link';
import { useOrganization } from '@/lib/organization-context';
import { createChatbot, ApiError } from '@/lib/api';
import type { ChatbotCreateInput } from '@/lib/types';

export default function NewChatbotPage() {
  const router = useRouter();
  const { currentOrg } = useOrganization();
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [formData, setFormData] = useState({
    name: '',
    websiteUrl: '',
    description: '',
    welcomeMessage: '',
    primaryColor: '#3b82f6',
    instructions: '',
  });

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (isSubmitting) return;

    if (!currentOrg?.id) {
      setError('No active organization selected. Please select or create an organization first.');
      return;
    }

    const trimmedName = formData.name.trim();
    if (!trimmedName) {
      setError('Chatbot name is required.');
      return;
    }

    const trimmedUrl = formData.websiteUrl.trim();
    if (trimmedUrl && !trimmedUrl.startsWith('http://') && !trimmedUrl.startsWith('https://')) {
      setError('Website URL must start with http:// or https://');
      return;
    }

    setIsSubmitting(true);
    setError(null);

    const payload: ChatbotCreateInput = {
      name: trimmedName,
      description: formData.description.trim() || null,
      website_url: trimmedUrl || null,
      welcome_message: formData.welcomeMessage.trim() || null,
      instructions: formData.instructions.trim() || null,
      primary_color: formData.primaryColor || '#3b82f6',
      status: 'draft',
    };

    try {
      const createdBot = await createChatbot(currentOrg.id, payload);
      router.push(`/chatbots/${createdBot.id}`);
    } catch (err: unknown) {
      const message =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : 'Failed to create chatbot';
      setError(message);
      setIsSubmitting(false);
    }
  };

  return (
    <div className="p-8 max-w-4xl mx-auto space-y-8">
      <div className="flex items-center gap-4">
        <Link
          href="/chatbots"
          className="inline-flex items-center justify-center h-10 w-10 rounded-lg text-slate-400 hover:text-slate-100 hover:bg-slate-900 transition-colors"
        >
          <ArrowLeft className="h-5 w-5" />
        </Link>
        <div>
          <h1 className="text-3xl font-bold tracking-tight text-slate-100">Create Chatbot</h1>
          <p className="text-slate-400 mt-1">Configure the details of your new AI agent.</p>
        </div>
      </div>

      <form onSubmit={handleSubmit} className="bg-slate-950 border border-slate-800 rounded-xl overflow-hidden">
        {error && (
          <div className="p-4 mx-6 mt-6 bg-red-950/40 border border-red-800/60 rounded-lg flex items-center gap-3 text-red-300 text-sm">
            <AlertCircle className="h-5 w-5 shrink-0 text-red-400" />
            <span>{error}</span>
          </div>
        )}

        <div className="p-6 sm:p-8 space-y-8">
          {/* Basic Info */}
          <div className="space-y-4 border-b border-slate-800 pb-8">
            <h2 className="text-lg font-medium text-slate-200">Basic Information</h2>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-6">
              <div className="space-y-2">
                <label htmlFor="name" className="text-sm font-medium text-slate-300">
                  Chatbot Name *
                </label>
                <input
                  id="name"
                  required
                  value={formData.name}
                  onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                  placeholder="e.g. Sales Assistant"
                  className="w-full bg-slate-900 border border-slate-700 rounded-lg px-4 py-2 text-sm text-slate-200 placeholder:text-slate-500 focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-all"
                />
              </div>
              <div className="space-y-2">
                <label htmlFor="websiteUrl" className="text-sm font-medium text-slate-300">
                  Website URL
                </label>
                <input
                  id="websiteUrl"
                  type="url"
                  value={formData.websiteUrl}
                  onChange={(e) => setFormData({ ...formData, websiteUrl: e.target.value })}
                  placeholder="https://example.com"
                  className="w-full bg-slate-900 border border-slate-700 rounded-lg px-4 py-2 text-sm text-slate-200 placeholder:text-slate-500 focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-all"
                />
              </div>
            </div>

            <div className="space-y-2">
              <label htmlFor="description" className="text-sm font-medium text-slate-300">
                Description
              </label>
              <input
                id="description"
                value={formData.description}
                onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                placeholder="What does this chatbot do?"
                className="w-full bg-slate-900 border border-slate-700 rounded-lg px-4 py-2 text-sm text-slate-200 placeholder:text-slate-500 focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-all"
              />
            </div>
          </div>

          {/* Behavior & Appearance */}
          <div className="space-y-4">
            <h2 className="text-lg font-medium text-slate-200">Behavior & Appearance</h2>

            <div className="space-y-2">
              <label htmlFor="welcomeMessage" className="text-sm font-medium text-slate-300">
                Welcome Message
              </label>
              <input
                id="welcomeMessage"
                value={formData.welcomeMessage}
                onChange={(e) => setFormData({ ...formData, welcomeMessage: e.target.value })}
                placeholder="Hi there! How can I help you today?"
                className="w-full bg-slate-900 border border-slate-700 rounded-lg px-4 py-2 text-sm text-slate-200 placeholder:text-slate-500 focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-all"
              />
            </div>

            <div className="space-y-2">
              <label htmlFor="instructions" className="text-sm font-medium text-slate-300">
                Basic Instructions
              </label>
              <textarea
                id="instructions"
                rows={4}
                value={formData.instructions}
                onChange={(e) => setFormData({ ...formData, instructions: e.target.value })}
                placeholder="Tell your chatbot how to behave, e.g. 'You are a helpful assistant. Keep answers brief.'"
                className="w-full bg-slate-900 border border-slate-700 rounded-lg px-4 py-2 text-sm text-slate-200 placeholder:text-slate-500 focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-all resize-y"
              />
            </div>

            <div className="space-y-2">
              <label htmlFor="primaryColor" className="text-sm font-medium text-slate-300">
                Primary Brand Color
              </label>
              <div className="flex items-center gap-3">
                <input
                  id="primaryColor"
                  type="color"
                  value={formData.primaryColor}
                  onChange={(e) => setFormData({ ...formData, primaryColor: e.target.value })}
                  className="h-10 w-10 rounded cursor-pointer border-0 p-0 bg-transparent"
                />
                <span className="text-sm text-slate-400 font-mono">{formData.primaryColor}</span>
              </div>
            </div>
          </div>
        </div>

        <div className="p-6 bg-slate-900/50 border-t border-slate-800 flex justify-end gap-3">
          <Link
            href="/chatbots"
            className="px-4 py-2 text-sm font-medium text-slate-300 hover:text-white transition-colors"
          >
            Cancel
          </Link>
          <button
            type="submit"
            disabled={isSubmitting || !currentOrg}
            className="inline-flex items-center gap-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed text-white px-6 py-2 rounded-lg font-medium transition-colors text-sm"
          >
            {isSubmitting ? (
              'Creating...'
            ) : (
              <>
                <Save className="h-4 w-4" />
                Create Chatbot
              </>
            )}
          </button>
        </div>
      </form>
    </div>
  );
}
