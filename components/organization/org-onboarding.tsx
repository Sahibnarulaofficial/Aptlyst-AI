'use client';

import React, { useState } from 'react';
import { Building2, Loader2, AlertCircle } from 'lucide-react';
import { useOrganization } from '@/lib/organization-context';

export function OrgOnboarding() {
  const { createOrganization, isCreating, createError, clearCreateError } = useOrganization();
  const [name, setName] = useState('');
  const [localError, setLocalError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (isCreating) return;

    const trimmed = name.trim();
    if (!trimmed) {
      setLocalError('Organization name is required');
      return;
    }

    setLocalError(null);
    clearCreateError();

    try {
      await createOrganization(trimmed);
    } catch {
      // Error handled in context createError state
    }
  }

  const displayError = localError || createError;

  return (
    <div className="min-h-[80vh] flex items-center justify-center px-4">
      <div className="w-full max-w-md space-y-6">
        <div className="flex flex-col items-center gap-3 text-center">
          <div className="h-12 w-12 rounded-xl bg-blue-600/10 border border-blue-500/20 flex items-center justify-center">
            <Building2 className="h-6 w-6 text-blue-500" />
          </div>
          <div>
            <h1 className="text-2xl font-bold text-slate-100 tracking-tight">Create your organization</h1>
            <p className="text-slate-400 text-sm mt-1">
              Set up your workspace to manage AI chatbots and knowledge bases.
            </p>
          </div>
        </div>

        <div className="bg-slate-950 border border-slate-800 rounded-xl p-6 shadow-xl">
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="space-y-1.5">
              <label htmlFor="org-name" className="text-sm font-medium text-slate-300">
                Organization Name
              </label>
              <input
                id="org-name"
                type="text"
                required
                maxLength={255}
                disabled={isCreating}
                value={name}
                onChange={(e) => {
                  setName(e.target.value);
                  if (displayError) {
                    setLocalError(null);
                    clearCreateError();
                  }
                }}
                className="w-full px-3 py-2 bg-slate-900 border border-slate-700 rounded-lg text-slate-100 placeholder-slate-500 text-sm focus:outline-none focus:ring-2 focus:ring-blue-600 focus:border-transparent transition disabled:opacity-50"
                placeholder="e.g. Acme Corp"
              />
            </div>

            {displayError && (
              <div className="flex items-start gap-2 text-sm text-red-400 bg-red-950/30 border border-red-900/40 rounded-lg p-3">
                <AlertCircle className="h-4 w-4 shrink-0 mt-0.5" />
                <span>{displayError}</span>
              </div>
            )}

            <button
              type="submit"
              disabled={isCreating || !name.trim()}
              className="w-full flex items-center justify-center gap-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-60 disabled:cursor-not-allowed text-white px-4 py-2.5 rounded-lg font-medium text-sm transition-colors"
            >
              {isCreating && <Loader2 className="h-4 w-4 animate-spin" />}
              {isCreating ? 'Creating organization…' : 'Create organization'}
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}
