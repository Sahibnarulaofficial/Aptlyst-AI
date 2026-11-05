'use client';

import { useState, useEffect, useCallback } from 'react';
import {
  CreditCard,
  Zap,
  AlertTriangle,
  AlertCircle,
  ExternalLink,
  Loader2,
  ShieldAlert,
  Calendar,
  Lock,
  RefreshCw,
  Info,
  Sparkles,
  Layers,
  X,
} from 'lucide-react';
import { useOrganization } from '@/lib/organization-context';
import {
  getBillingSubscription,
  createCheckoutSession,
  createBillingPortalSession,
  ApiError,
} from '@/lib/api';
import type { BillingSubscriptionOut, BillingPeriodOut } from '@/lib/types';

function formatDate(isoString?: string | null): string {
  if (!isoString) return '—';
  try {
    const date = new Date(isoString);
    if (isNaN(date.getTime())) return '—';
    return date.toLocaleDateString(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
    });
  } catch {
    return '—';
  }
}

function formatTokens(num?: number | null): string {
  if (num === null || num === undefined) return '—';
  return num.toLocaleString();
}

export default function BillingPage() {
  const { currentOrg, isLoading: isOrgLoading } = useOrganization();

  const [subscription, setSubscription] = useState<BillingSubscriptionOut | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(Boolean(currentOrg?.id));
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [isUpgrading, setIsUpgrading] = useState<boolean>(false);
  const [isPortalLoading, setIsPortalLoading] = useState<boolean>(false);

  // Synchronize state during render when organization changes (prevents stale leaks)
  const [prevOrgId, setPrevOrgId] = useState(currentOrg?.id);
  if (prevOrgId !== currentOrg?.id) {
    setPrevOrgId(currentOrg?.id);
    setSubscription(null);
    setFetchError(null);
    setActionError(null);
    setIsLoading(Boolean(currentOrg?.id));
  }

  // Load subscription details for the active organization
  useEffect(() => {
    if (!currentOrg?.id) {
      return;
    }

    const orgId = currentOrg.id;
    let ignore = false;

    async function load(targetOrgId: string) {
      setIsLoading(true);
      setFetchError(null);
      try {
        const data = await getBillingSubscription(targetOrgId);
        if (!ignore) {
          setSubscription(data);
        }
      } catch (err: unknown) {
        if (!ignore) {
          setSubscription(null);
          const message =
            err instanceof ApiError
              ? err.message
              : err instanceof Error
                ? err.message
                : 'Failed to load billing information';
          setFetchError(message);
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

  const orgId = currentOrg?.id;
  const handleRetry = useCallback(async () => {
    if (!orgId) return;
    setIsLoading(true);
    setFetchError(null);
    setActionError(null);
    try {
      const data = await getBillingSubscription(orgId);
      setSubscription(data);
    } catch (err: unknown) {
      setSubscription(null);
      const message =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : 'Failed to load billing information';
      setFetchError(message);
    } finally {
      setIsLoading(false);
    }
  }, [orgId]);

  const role = currentOrg?.role;
  const canManageBilling = role === 'owner' || role === 'admin';

  // Handle Checkout Upgrade
  const handleUpgrade = async () => {
    if (!currentOrg?.id || !canManageBilling || isUpgrading || isPortalLoading) return;
    setIsUpgrading(true);
    setActionError(null);
    try {
      const session = await createCheckoutSession(currentOrg.id);
      if (session?.checkout_url) {
        window.location.assign(session.checkout_url);
      } else {
        setActionError('The payment provider did not return a valid checkout redirect URL.');
        setIsUpgrading(false);
      }
    } catch (err: unknown) {
      setIsUpgrading(false);
      if (err instanceof ApiError) {
        if (err.status === 409) {
          setActionError(
            err.message || 'An active subscription or checkout attempt is already in progress for this organization.'
          );
        } else if (err.status === 503) {
          setActionError(
            err.message || 'Stripe billing is currently disabled or temporarily unavailable.'
          );
        } else {
          setActionError(err.message || 'Failed to initiate checkout session.');
        }
      } else {
        setActionError('An unexpected error occurred while initiating checkout. Please try again.');
      }
    }
  };

  // Handle Billing Portal
  const handleManageBilling = async () => {
    if (!currentOrg?.id || !canManageBilling || isUpgrading || isPortalLoading) return;
    setIsPortalLoading(true);
    setActionError(null);
    try {
      const portal = await createBillingPortalSession(currentOrg.id);
      if (portal?.portal_url) {
        window.location.assign(portal.portal_url);
      } else {
        setActionError('The payment provider did not return a valid portal redirect URL.');
        setIsPortalLoading(false);
      }
    } catch (err: unknown) {
      setIsPortalLoading(false);
      if (err instanceof ApiError) {
        if (err.status === 404) {
          setActionError('No billing customer found for this organization.');
        } else if (err.status === 503) {
          setActionError(
            err.message || 'Stripe customer portal is currently unavailable.'
          );
        } else {
          setActionError(err.message || 'Failed to access billing portal.');
        }
      } else {
        setActionError('An unexpected error occurred while accessing the billing portal. Please try again.');
      }
    }
  };

  // Subscription calculation
  const isPro = subscription?.plan_tier?.toLowerCase() === 'pro';
  const planName = isPro ? 'Pro Plan' : 'Free Plan';
  const statusStr = (subscription?.status || 'active').toLowerCase();

  // Token usage calculation
  const bp: BillingPeriodOut | null = subscription?.billing_period ?? null;
  const quota = bp?.monthly_token_quota ?? 0;
  const used = bp?.tokens_used ?? 0;
  const reserved = bp?.tokens_reserved ?? 0;
  const remaining = bp?.tokens_remaining ?? 0;

  // Safe percentage calculation:
  // - Handle zero quota
  // - Prevent negative percentages
  // - Never display more than 100% progress
  const rawPercentage = quota > 0 ? (used / quota) * 100 : 0;
  const percentage = Math.min(100, Math.max(0, Math.round(rawPercentage)));

  const isExhausted = percentage >= 100 || Boolean(bp?.is_blocked);
  const isWarning = percentage >= 80 && !isExhausted;

  const progressBarColor = isExhausted
    ? 'bg-rose-500'
    : isWarning
      ? 'bg-amber-500'
      : 'bg-blue-600';

  // Status Badge Rendering
  const renderStatusBadge = (status: string) => {
    switch (status) {
      case 'active':
        return (
          <span className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
            Active
          </span>
        );
      case 'trialing':
        return (
          <span className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium bg-amber-500/10 text-amber-400 border border-amber-500/20">
            <span className="h-1.5 w-1.5 rounded-full bg-amber-400" />
            Trialing
          </span>
        );
      case 'past_due':
        return (
          <span className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium bg-amber-500/10 text-amber-400 border border-amber-500/20">
            <AlertTriangle className="h-3 w-3" />
            Past Due
          </span>
        );
      case 'canceled':
        return (
          <span className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium bg-rose-500/10 text-rose-400 border border-rose-500/20">
            <span className="h-1.5 w-1.5 rounded-full bg-rose-400" />
            Canceled
          </span>
        );
      case 'incomplete':
        return (
          <span className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium bg-yellow-500/10 text-yellow-400 border border-yellow-500/20">
            <span className="h-1.5 w-1.5 rounded-full bg-yellow-400" />
            Incomplete
          </span>
        );
      default:
        return (
          <span className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium bg-slate-800 text-slate-300 border border-slate-700 capitalize">
            {status}
          </span>
        );
    }
  };

  return (
    <div className="p-8 max-w-7xl mx-auto space-y-8">
      {/* ─── Header ──────────────────────────────────────────────────────── */}
      <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4">
        <div>
          <h1 className="text-3xl font-bold tracking-tight text-slate-100">Billing & Plans</h1>
          <p className="text-slate-400 mt-1">
            Manage your subscription tier, billing details, and LLM token quotas
            {currentOrg?.name && (
              <>
                {' '}for <span className="text-slate-200 font-medium">{currentOrg.name}</span>
              </>
            )}
            .
          </p>
        </div>

        {/* Header shortcut for portal if subscribed */}
        {subscription?.has_stripe_customer && canManageBilling && !isLoading && !fetchError && (
          <button
            type="button"
            onClick={handleManageBilling}
            disabled={isPortalLoading || isUpgrading}
            className="inline-flex items-center gap-2 border border-slate-700 hover:bg-slate-800 text-slate-200 px-4 py-2 rounded-lg text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed shrink-0"
          >
            {isPortalLoading ? (
              <Loader2 className="h-4 w-4 animate-spin text-blue-400" />
            ) : (
              <ExternalLink className="h-4 w-4 text-slate-400" />
            )}
            Stripe Portal
          </button>
        )}
      </div>

      {/* ─── Action Error Alert ─────────────────────────────────────────── */}
      {actionError && (
        <div className="bg-rose-500/10 border border-rose-500/20 rounded-xl p-4 flex items-start justify-between gap-3 text-rose-300 text-sm">
          <div className="flex items-start gap-3">
            <AlertCircle className="h-5 w-5 text-rose-400 shrink-0 mt-0.5" />
            <div>
              <p className="font-semibold text-rose-200">Billing Action Failed</p>
              <p className="mt-0.5 text-rose-300">{actionError}</p>
            </div>
          </div>
          <button
            type="button"
            onClick={() => setActionError(null)}
            className="text-rose-400 hover:text-rose-200 p-1 rounded-md transition-colors"
            aria-label="Dismiss error"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      )}

      {/* ─── Blocked Banner ─────────────────────────────────────────────── */}
      {bp?.is_blocked && (
        <div className="bg-rose-500/10 border border-rose-500/30 rounded-xl p-4 flex items-start gap-3 text-sm">
          <ShieldAlert className="h-5 w-5 text-rose-400 shrink-0 mt-0.5" />
          <div className="space-y-1">
            <h3 className="font-semibold text-rose-200">Account LLM Access Blocked</h3>
            <p className="text-rose-300">
              {bp.blocked_reason || 'Monthly token quota exhausted. Token reservations are suspended.'}
            </p>
            {canManageBilling && !isPro && (
              <p className="text-rose-200 text-xs mt-2">
                Upgrade your subscription or contact an administrator to restore access.
              </p>
            )}
          </div>
        </div>
      )}

      {/* ─── Past Due Banner ────────────────────────────────────────────── */}
      {statusStr === 'past_due' && (
        <div className="bg-amber-500/10 border border-amber-500/20 rounded-xl p-4 flex items-start gap-3 text-sm">
          <AlertTriangle className="h-5 w-5 text-amber-400 shrink-0 mt-0.5" />
          <div className="space-y-1">
            <h3 className="font-semibold text-amber-200">Payment Past Due</h3>
            <p className="text-amber-300">
              Your latest payment could not be processed
              {subscription?.past_due_since && (
                <> since {formatDate(subscription.past_due_since)}</>
              )}
              . Please update your payment method in the Billing Portal to prevent service interruption.
            </p>
          </div>
        </div>
      )}

      {/* ─── Cancel at Period End Banner ────────────────────────────────── */}
      {subscription?.cancel_at_period_end && (
        <div className="bg-amber-500/10 border border-amber-500/20 rounded-xl p-4 flex items-start gap-3 text-sm">
          <Calendar className="h-5 w-5 text-amber-400 shrink-0 mt-0.5" />
          <div className="space-y-1">
            <h3 className="font-semibold text-amber-200">Subscription Scheduled for Cancellation</h3>
            <p className="text-amber-300">
              Your Pro subscription will automatically end at the conclusion of your current billing period on{' '}
              <span className="font-medium text-amber-100">{formatDate(subscription.current_period_end)}</span>.
              You will continue to have full Pro privileges until then.
            </p>
          </div>
        </div>
      )}

      {/* ─── Member Role Notice ─────────────────────────────────────────── */}
      {!canManageBilling && (
        <div className="bg-slate-950 border border-slate-800 rounded-xl p-4 flex items-center gap-3 text-slate-400 text-sm">
          <Info className="h-5 w-5 text-slate-500 shrink-0" />
          <span>
            You are viewing billing details as a member. Only organization owners and administrators can change billing plans or access the Stripe Billing Portal.
          </span>
        </div>
      )}

      {/* ─── Disabled Stripe Banner ─────────────────────────────────────── */}
      {subscription && !subscription.stripe_enabled && canManageBilling && (
        <div className="bg-slate-950 border border-slate-800 rounded-xl p-4 flex items-center gap-3 text-slate-400 text-sm">
          <Lock className="h-5 w-5 text-slate-500 shrink-0" />
          <span>
            Stripe billing is currently disabled or in mock mode on this instance. Plan upgrades and billing portal sessions are unavailable.
          </span>
        </div>
      )}

      {/* ─── Main Content / Loading / Error ─────────────────────────────── */}
      {isLoading || isOrgLoading ? (
        <div className="space-y-6">
          {/* Skeletons */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            <div className="bg-slate-950 border border-slate-800 rounded-xl p-6 h-52 animate-pulse flex flex-col justify-between">
              <div className="space-y-3">
                <div className="h-4 w-28 bg-slate-800 rounded" />
                <div className="h-8 w-44 bg-slate-800 rounded" />
              </div>
              <div className="h-4 w-3/4 bg-slate-800 rounded" />
            </div>
            <div className="bg-slate-950 border border-slate-800 rounded-xl p-6 h-52 animate-pulse flex flex-col justify-between">
              <div className="space-y-3">
                <div className="h-4 w-32 bg-slate-800 rounded" />
                <div className="h-8 w-40 bg-slate-800 rounded" />
              </div>
              <div className="h-4 w-full bg-slate-800 rounded" />
            </div>
          </div>
          <div className="bg-slate-950 border border-slate-800 rounded-xl p-6 h-72 animate-pulse" />
        </div>
      ) : fetchError ? (
        <div className="bg-slate-950 border border-slate-800 rounded-xl p-8 text-center space-y-4">
          <div className="h-12 w-12 rounded-full bg-rose-500/10 border border-rose-500/20 text-rose-400 flex items-center justify-center mx-auto">
            <AlertCircle className="h-6 w-6" />
          </div>
          <div className="space-y-1">
            <h3 className="text-lg font-semibold text-slate-100">Unable to Load Billing Information</h3>
            <p className="text-slate-400 text-sm max-w-md mx-auto">{fetchError}</p>
          </div>
          <button
            type="button"
            onClick={handleRetry}
            className="inline-flex items-center gap-2 bg-slate-800 hover:bg-slate-700 text-slate-200 px-4 py-2 rounded-lg text-sm font-medium transition-colors"
          >
            <RefreshCw className="h-4 w-4" />
            Retry
          </button>
        </div>
      ) : (
        <div className="space-y-8">
          {/* ─── Top Cards Grid ───────────────────────────────────────── */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            {/* Subscription Overview Card */}
            <div className="bg-slate-950 border border-slate-800 rounded-xl p-6 flex flex-col justify-between gap-6">
              <div className="space-y-4">
                <div className="flex items-center justify-between">
                  <h3 className="text-sm font-medium text-slate-400">Current Subscription</h3>
                  {renderStatusBadge(statusStr)}
                </div>

                <div>
                  <div className="flex items-center gap-2">
                    <span className="text-2xl font-bold text-slate-100">{planName}</span>
                    {isPro && (
                      <span className="bg-blue-500/10 text-blue-400 text-xs px-2 py-0.5 rounded-full font-medium border border-blue-500/20">
                        Pro Tier
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-slate-400 mt-1">
                    {isPro
                      ? 'Pro subscription tier for your organization.'
                      : 'Default free subscription tier for your organization.'}
                  </p>
                </div>

                {/* Period Dates */}
                <div className="pt-3 border-t border-slate-800/80 space-y-2 text-xs text-slate-400">
                  <div className="flex justify-between">
                    <span>Billing cycle:</span>
                    <span className="text-slate-300 font-medium">
                      {subscription?.current_period_start && subscription?.current_period_end
                        ? `${formatDate(subscription.current_period_start)} – ${formatDate(subscription.current_period_end)}`
                        : 'Standard monthly'}
                    </span>
                  </div>
                  {subscription?.current_period_end && (
                    <div className="flex justify-between">
                      <span>Next renewal:</span>
                      <span className="text-slate-300 font-medium">
                        {subscription.cancel_at_period_end
                          ? `Expires on ${formatDate(subscription.current_period_end)}`
                          : formatDate(subscription.current_period_end)}
                      </span>
                    </div>
                  )}
                </div>
              </div>

              {/* Card Actions */}
              <div className="pt-4 border-t border-slate-800 flex flex-wrap items-center gap-3">
                {!isPro && canManageBilling && (
                  <button
                    type="button"
                    onClick={handleUpgrade}
                    disabled={isUpgrading || isPortalLoading || !subscription?.stripe_enabled}
                    className="inline-flex items-center gap-2 bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg font-medium transition-colors text-sm disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    {isUpgrading ? (
                      <Loader2 className="h-4 w-4 animate-spin" />
                    ) : (
                      <Zap className="h-4 w-4" />
                    )}
                    Upgrade to Pro
                  </button>
                )}

                {subscription?.has_stripe_customer && canManageBilling && (
                  <button
                    type="button"
                    onClick={handleManageBilling}
                    disabled={isPortalLoading || isUpgrading || !subscription?.stripe_enabled}
                    className="inline-flex items-center gap-2 border border-slate-700 hover:bg-slate-800 text-slate-200 px-4 py-2 rounded-lg text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    {isPortalLoading ? (
                      <Loader2 className="h-4 w-4 animate-spin text-blue-400" />
                    ) : (
                      <CreditCard className="h-4 w-4 text-slate-400" />
                    )}
                    Manage in Stripe Portal
                  </button>
                )}
              </div>
            </div>

            {/* Token Usage Card */}
            <div className="bg-slate-950 border border-slate-800 rounded-xl p-6 flex flex-col justify-between gap-6">
              <div className="space-y-4">
                <div className="flex items-center justify-between">
                  <h3 className="text-sm font-medium text-slate-400">Monthly LLM Token Quota</h3>
                  {bp && (
                    <span className="text-xs text-slate-400 flex items-center gap-1">
                      <Calendar className="h-3 w-3" />
                      Resets {formatDate(bp.period_end)}
                    </span>
                  )}
                </div>

                {bp ? (
                  <div className="space-y-3">
                    <div>
                      <div className="flex items-baseline justify-between">
                        <span className="text-2xl font-bold text-slate-100">
                          {formatTokens(used)}
                        </span>
                        <span className="text-xs text-slate-400 font-medium">
                          of {formatTokens(quota)} tokens ({percentage}%)
                        </span>
                      </div>
                      <p className="text-xs text-slate-400 mt-0.5">
                        {isExhausted
                          ? 'Token quota reached. LLM inference suspended until renewal or upgrade.'
                          : isWarning
                            ? 'Approaching monthly quota limit.'
                            : 'Tokens consumed across all active chatbot conversations.'}
                      </p>
                    </div>

                    {/* Progress Bar */}
                    <div className="w-full bg-slate-800/80 rounded-full h-2.5 overflow-hidden">
                      <div
                        className={`h-2.5 rounded-full transition-all duration-300 ${progressBarColor}`}
                        style={{ width: `${percentage}%` }}
                      />
                    </div>

                    {/* Breakdown metrics */}
                    <div className="grid grid-cols-2 gap-3 pt-3 border-t border-slate-800/80 text-xs">
                      <div>
                        <span className="text-slate-400 block">Remaining</span>
                        <span className="text-sm font-semibold text-slate-200">
                          {formatTokens(remaining)}
                        </span>
                      </div>
                      <div>
                        <span className="text-slate-400 block">In-Flight Reserved</span>
                        <span className="text-sm font-semibold text-slate-200">
                          {formatTokens(reserved)}
                        </span>
                      </div>
                    </div>
                  </div>
                ) : (
                  <div className="py-6 text-center space-y-2">
                    <Layers className="h-8 w-8 text-slate-600 mx-auto" />
                    <p className="text-sm text-slate-300 font-medium">No Active Billing Period</p>
                    <p className="text-xs text-slate-400 max-w-sm mx-auto">
                      No active billing period is currently recorded for this organization. Usage accounting will begin once LLM operations are initiated or a plan is activated.
                    </p>
                  </div>
                )}
              </div>

              {/* Status Note */}
              <div className="pt-3 border-t border-slate-800 text-xs text-slate-400 flex items-center justify-between">
                <span>Accounting Status:</span>
                <span className="text-slate-300 font-medium">
                  {bp?.is_blocked ? (
                    <span className="text-rose-400 font-semibold">Blocked</span>
                  ) : bp ? (
                    <span className="text-emerald-400 font-semibold">Healthy</span>
                  ) : (
                    'Uninitialized'
                  )}
                </span>
              </div>
            </div>
          </div>

          {/* ─── Plan Comparison Section ──────────────────────────────── */}
          <div className="space-y-4">
            <div>
              <h2 className="text-xl font-bold tracking-tight text-slate-100">Subscription Plans</h2>
              <p className="text-slate-400 text-sm mt-0.5">
                Overview of available subscription tiers for your organization.
              </p>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              {/* Free Tier Card */}
              <div
                className={`bg-slate-950 border rounded-xl p-6 flex flex-col justify-between gap-6 ${
                  !isPro ? 'border-slate-700 ring-1 ring-slate-700' : 'border-slate-800'
                }`}
              >
                <div className="space-y-4">
                  <div className="flex items-center justify-between">
                    <div>
                      <h3 className="text-lg font-bold text-slate-100">Free Tier</h3>
                      <p className="text-xs text-slate-400 mt-0.5">Current free subscription state</p>
                    </div>
                    {!isPro && (
                      <span className="text-xs font-semibold px-2.5 py-1 rounded-md bg-slate-800 text-slate-300 border border-slate-700">
                        Current Plan
                      </span>
                    )}
                  </div>

                  <div className="space-y-3 text-xs text-slate-400 pt-2">
                    <p className="text-slate-300">
                      Standard organization tier assigned at creation.
                    </p>
                    <p>
                      Pricing and detailed plan limits are determined by backend configuration and are not yet available.
                    </p>
                  </div>
                </div>

                <div className="pt-4 border-t border-slate-800">
                  <div className="text-xs text-slate-500">
                    Active by default for all registered organizations.
                  </div>
                </div>
              </div>

              {/* Pro Tier Card */}
              <div
                className={`bg-slate-950 border rounded-xl p-6 flex flex-col justify-between gap-6 relative overflow-hidden ${
                  isPro
                    ? 'border-blue-500/50 ring-1 ring-blue-500/30'
                    : 'border-slate-800 hover:border-slate-700 transition-colors'
                }`}
              >
                {/* Subtle highlight gradient */}
                <div className="absolute top-0 right-0 -mt-4 -mr-4 w-24 h-24 bg-blue-600/10 rounded-full blur-xl pointer-events-none" />

                <div className="space-y-4">
                  <div className="flex items-center justify-between">
                    <div>
                      <div className="flex items-center gap-2">
                        <h3 className="text-lg font-bold text-slate-100">Pro Tier</h3>
                        <Sparkles className="h-4 w-4 text-blue-400" />
                      </div>
                      <p className="text-xs text-slate-400 mt-0.5">Paid subscription option</p>
                    </div>
                    {isPro ? (
                      <span className="text-xs font-semibold px-2.5 py-1 rounded-md bg-blue-500/10 text-blue-400 border border-blue-500/20">
                        Current Plan
                      </span>
                    ) : (
                      <span className="text-xs font-semibold px-2.5 py-1 rounded-md bg-slate-800 text-slate-300">
                        Available
                      </span>
                    )}
                  </div>

                  <div className="space-y-3 text-xs text-slate-400 pt-2">
                    <p className="text-slate-300">
                      Paid subscription option managed through Stripe billing.
                    </p>
                    <p>
                      Pricing, token quotas, and entitlements are determined by backend configuration. Detailed plan specifications and limits are not yet published.
                    </p>
                  </div>
                </div>

                <div className="pt-4 border-t border-slate-800">
                  {isPro ? (
                    subscription?.has_stripe_customer && canManageBilling ? (
                      <button
                        type="button"
                        onClick={handleManageBilling}
                        disabled={isPortalLoading || isUpgrading || !subscription?.stripe_enabled}
                        className="w-full inline-flex items-center justify-center gap-2 border border-slate-700 hover:bg-slate-800 text-slate-200 px-4 py-2 rounded-lg text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                      >
                        {isPortalLoading ? (
                          <Loader2 className="h-4 w-4 animate-spin text-blue-400" />
                        ) : (
                          <ExternalLink className="h-4 w-4 text-slate-400" />
                        )}
                        Manage Subscription & Invoices
                      </button>
                    ) : (
                      <div className="text-xs text-slate-400 text-center py-2">
                        Active on Pro Plan
                      </div>
                    )
                  ) : canManageBilling ? (
                    <button
                      type="button"
                      onClick={handleUpgrade}
                      disabled={isUpgrading || isPortalLoading || !subscription?.stripe_enabled}
                      className="w-full inline-flex items-center justify-center gap-2 bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg font-medium transition-colors text-sm disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      {isUpgrading ? (
                        <Loader2 className="h-4 w-4 animate-spin" />
                      ) : (
                        <Zap className="h-4 w-4" />
                      )}
                      Upgrade to Pro Plan
                    </button>
                  ) : (
                    <div className="text-xs text-slate-400 text-center py-2">
                      Contact an organization owner or administrator to upgrade.
                    </div>
                  )}
                </div>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
