'use client';

import React, { useState, useRef, useEffect } from 'react';
import Link from 'next/link';
import {
  Building2,
  ShieldCheck,
  ChevronDown,
  Check,
  Plus,
  X,
  AlertCircle,
  Loader2,
  ArrowUpRight,
} from 'lucide-react';
import { useOrganization } from '@/lib/organization-context';
import { ApiError } from '@/lib/api';
import { createClient } from '@/lib/supabase/client';
import { cn } from '@/lib/utils';

function isPaidEntitlement(appMetadata?: Record<string, unknown> | null): boolean {
  if (!appMetadata) return false;

  const isPaidVal = appMetadata.is_paid;
  const rawPlan =
    typeof appMetadata.plan === 'string' ? appMetadata.plan.trim().toLowerCase() : 'free';
  const rawStatus =
    typeof appMetadata.subscription_status === 'string'
      ? appMetadata.subscription_status.trim().toLowerCase()
      : null;

  const inactiveStatuses = new Set([
    'canceled',
    'past_due',
    'unpaid',
    'incomplete',
    'incomplete_expired',
  ]);

  // Explicit revocation
  if (isPaidVal === false) return false;
  if (rawStatus && inactiveStatuses.has(rawStatus)) return false;

  // Positive grants
  if (isPaidVal === true) return true;
  if (rawPlan === 'pro' || rawPlan === 'enterprise') {
    if (!rawStatus || rawStatus === 'active' || rawStatus === 'trialing') {
      return true;
    }
  }

  return false;
}

export function OrgSwitcher() {
  const {
    organizations,
    currentOrg,
    setCurrentOrgId,
    createOrganization,
    isCreating,
  } = useOrganization();

  const [isOpen, setIsOpen] = useState(false);
  const [isCreateOpen, setIsCreateOpen] = useState(false);
  const [newOrgName, setNewOrgName] = useState('');
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [isPaidPlan, setIsPaidPlan] = useState<boolean | null>(null);

  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let ignore = false;
    async function checkPlan() {
      try {
        const supabase = createClient();
        const { data } = await supabase.auth.getSession();
        if (ignore) return;
        const appMetadata = data.session?.user?.app_metadata;
        setIsPaidPlan(isPaidEntitlement(appMetadata));
      } catch {
        // Fail-closed on lookup failure
        if (!ignore) setIsPaidPlan(false);
      }
    }
    checkPlan();
    return () => {
      ignore = true;
    };
  }, []);

  useEffect(() => {
    if (!isOpen) return;

    function handleClickOutside(event: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
        setIsOpen(false);
      }
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        setIsOpen(false);
      }
    }

    document.addEventListener('mousedown', handleClickOutside);
    document.addEventListener('keydown', handleKeyDown);

    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [isOpen]);

  useEffect(() => {
    if (!isCreateOpen) return;

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape' && !isCreating) {
        setIsCreateOpen(false);
        setDialogError(null);
      }
    }

    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [isCreateOpen, isCreating]);

  if (!currentOrg) return null;

  const ownedOrgCount = organizations.filter((org) => org.role === 'owner').length;
  const isOwner = ownedOrgCount >= 1;

  // Fail-closed: An owner without confirmed paid status is treated as at the limit.
  const isLimitReached =
    (isOwner && isPaidPlan !== true) ||
    Boolean(
      dialogError &&
        (dialogError.toLowerCase().includes('free plan') ||
          dialogError.toLowerCase().includes('upgrade') ||
          dialogError.toLowerCase().includes('limit')),
    );

  async function handleCreateSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (isCreating) return;

    if (isLimitReached) {
      setDialogError(
        'Free plan is limited to 1 organization. Upgrade to create more organizations.',
      );
      return;
    }

    const trimmed = newOrgName.trim();
    if (!trimmed) {
      setDialogError('Organization name is required.');
      return;
    }

    setDialogError(null);

    try {
      await createOrganization(trimmed);
      setIsCreateOpen(false);
      setNewOrgName('');
    } catch (err: unknown) {
      const message =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : 'Failed to create organization';
      setDialogError(message);
    }
  }

  return (
    <div ref={containerRef} className="relative px-3 py-2 border-b border-slate-800 bg-slate-900/40">
      <button
        type="button"
        onClick={() => setIsOpen((prev) => !prev)}
        aria-expanded={isOpen}
        aria-haspopup="listbox"
        aria-controls="org-switcher-menu"
        className={cn(
          'w-full flex items-center justify-between gap-2 p-1.5 rounded-lg transition-colors text-left border border-transparent',
          'hover:bg-slate-800/70 hover:border-slate-700/60 focus:outline-none focus:ring-1 focus:ring-blue-500',
          isOpen && 'bg-slate-800/80 border-slate-700',
        )}
      >
        <div className="flex items-center gap-2 min-w-0">
          <div className="h-7 w-7 rounded bg-slate-800 border border-slate-700 flex items-center justify-center shrink-0">
            <Building2 className="h-4 w-4 text-slate-300" />
          </div>
          <div className="flex flex-col min-w-0">
            <span className="text-xs font-semibold text-slate-200 truncate leading-tight">
              {currentOrg.name}
            </span>
            <span className="text-[10px] text-slate-400 capitalize flex items-center gap-1 mt-0.5">
              <ShieldCheck className="h-2.5 w-2.5 text-blue-400" />
              {currentOrg.role}
            </span>
          </div>
        </div>
        <ChevronDown
          className={cn(
            'h-4 w-4 text-slate-400 shrink-0 transition-transform duration-150',
            isOpen && 'rotate-180 text-slate-200',
          )}
        />
      </button>

      {isOpen && (
        <div
          id="org-switcher-menu"
          role="listbox"
          aria-label="Select organization"
          className="absolute top-full left-2 right-2 mt-1.5 z-40 bg-slate-950 border border-slate-800 rounded-lg shadow-xl p-1.5 space-y-1 animate-in fade-in-50 duration-100"
        >
          <div className="px-2 py-1 text-[10px] font-semibold uppercase tracking-wider text-slate-500">
            Organizations ({organizations.length})
          </div>

          <div className="space-y-0.5 max-h-56 overflow-y-auto">
            {organizations.map((org) => {
              const isSelected = org.id === currentOrg.id;
              return (
                <button
                  key={org.id}
                  role="option"
                  aria-selected={isSelected}
                  type="button"
                  onClick={() => {
                    setCurrentOrgId(org.id);
                    setIsOpen(false);
                  }}
                  className={cn(
                    'w-full flex items-center justify-between gap-2 px-2.5 py-2 rounded-md text-xs transition-colors text-left',
                    isSelected
                      ? 'bg-blue-600/15 text-blue-300 border border-blue-500/25'
                      : 'text-slate-300 hover:bg-slate-900 hover:text-slate-100 border border-transparent',
                  )}
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <Building2
                      className={cn(
                        'h-3.5 w-3.5 shrink-0',
                        isSelected ? 'text-blue-400' : 'text-slate-400',
                      )}
                    />
                    <div className="flex flex-col min-w-0">
                      <span className="font-medium truncate">{org.name}</span>
                      <span className="text-[10px] text-slate-500 capitalize">{org.role}</span>
                    </div>
                  </div>
                  {isSelected && <Check className="h-3.5 w-3.5 text-blue-400 shrink-0" />}
                </button>
              );
            })}
          </div>

          {organizations.length === 1 && (
            <div className="px-2 pt-2 pb-1 border-t border-slate-800/80 text-[11px] text-slate-500">
              Only 1 organization available.{' '}
              {isOwner && isPaidPlan !== true
                ? 'Upgrade to create more organizations.'
                : 'Create another organization to switch.'}
            </div>
          )}

          <div className="pt-1 mt-1 border-t border-slate-800">
            <button
              type="button"
              onClick={() => {
                setIsOpen(false);
                setIsCreateOpen(true);
                setNewOrgName('');
                setDialogError(null);
              }}
              className="w-full flex items-center gap-2 px-2.5 py-1.5 rounded-md text-xs text-blue-400 hover:text-blue-300 hover:bg-slate-900 transition-colors text-left font-medium"
            >
              <Plus className="h-3.5 w-3.5 shrink-0" />
              <span>Create organization</span>
            </button>
          </div>
        </div>
      )}

      {isCreateOpen && (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="create-org-title"
          className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm"
        >
          <div className="w-full max-w-sm bg-slate-950 border border-slate-800 rounded-xl p-5 shadow-2xl space-y-4 animate-in fade-in-50 zoom-in-95 duration-150">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <div className="h-7 w-7 rounded-lg bg-blue-600/10 border border-blue-500/20 flex items-center justify-center">
                  <Building2 className="h-4 w-4 text-blue-400" />
                </div>
                <h2 id="create-org-title" className="text-sm font-semibold text-slate-100">
                  Create organization
                </h2>
              </div>
              <button
                type="button"
                onClick={() => {
                  if (!isCreating) {
                    setIsCreateOpen(false);
                    setDialogError(null);
                  }
                }}
                disabled={isCreating}
                className="text-slate-400 hover:text-slate-200 p-1 rounded-md transition-colors disabled:opacity-50"
                aria-label="Close dialog"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            {isLimitReached ? (
              <div
                data-testid="upgrade-prompt-card"
                className="p-3 bg-amber-950/40 border border-amber-800/60 rounded-xl space-y-2 text-xs"
              >
                <div className="flex items-start gap-2 text-amber-300 font-medium">
                  <AlertCircle className="h-4 w-4 shrink-0 text-amber-400 mt-0.5" />
                  <span>Free plan limit reached</span>
                </div>
                <p className="text-slate-300 pl-6 text-[11px] leading-relaxed">
                  Free accounts can own up to 1 organization. To create and manage additional
                  organizations, please contact your administrator to provision a paid plan (self-serve billing is in preview).
                </p>
                <div className="pl-6 pt-1">
                  <Link
                    href="/billing"
                    onClick={() => {
                      setIsCreateOpen(false);
                      setDialogError(null);
                    }}
                    className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-blue-600 hover:bg-blue-700 text-white font-medium text-xs transition-colors"
                  >
                    <span>View Billing Plans</span>
                    <ArrowUpRight className="h-3.5 w-3.5" />
                  </Link>
                </div>
              </div>
            ) : (
              <p className="text-xs text-slate-400">
                Enter a name for your new organization workspace.
              </p>
            )}

            {dialogError && !isLimitReached && (
              <div className="p-2.5 bg-red-950/40 border border-red-800/60 rounded-lg flex items-start gap-2 text-red-300 text-xs">
                <AlertCircle className="h-4 w-4 shrink-0 text-red-400 mt-0.5" />
                <span>{dialogError}</span>
              </div>
            )}

            <form onSubmit={handleCreateSubmit} className="space-y-4">
              <div className="space-y-1.5">
                <label htmlFor="new-org-name" className="text-xs font-medium text-slate-300">
                  Organization name *
                </label>
                <input
                  id="new-org-name"
                  type="text"
                  required
                  autoFocus
                  maxLength={255}
                  disabled={isCreating || isLimitReached}
                  value={newOrgName}
                  onChange={(e) => {
                    setNewOrgName(e.target.value);
                    if (dialogError) setDialogError(null);
                  }}
                  placeholder="e.g. Acme Corp"
                  className="w-full bg-slate-900 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-slate-200 placeholder:text-slate-500 focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-all disabled:opacity-50"
                />
              </div>

              <div className="flex items-center justify-end gap-2 pt-1">
                <button
                  type="button"
                  disabled={isCreating}
                  onClick={() => {
                    setIsCreateOpen(false);
                    setDialogError(null);
                  }}
                  className="px-3 py-1.5 text-xs font-medium text-slate-400 hover:text-slate-200 hover:bg-slate-900 rounded-lg transition-colors disabled:opacity-50"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={isCreating || !newOrgName.trim() || isLimitReached}
                  className="inline-flex items-center gap-1.5 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed text-white px-3.5 py-1.5 rounded-lg font-medium transition-colors text-xs"
                >
                  {isCreating ? (
                    <>
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      <span>Creating…</span>
                    </>
                  ) : isLimitReached ? (
                    <span>Upgrade required</span>
                  ) : (
                    <span>Create organization</span>
                  )}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
