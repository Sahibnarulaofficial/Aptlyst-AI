'use client';

import React from 'react';
import { Loader2, AlertCircle } from 'lucide-react';
import { useOrganization } from '@/lib/organization-context';
import { OrgOnboarding } from '@/components/organization/org-onboarding';

export function OrganizationGate({ children }: { children: React.ReactNode }) {
  const { organizations, isLoading, error, refetchOrganizations } = useOrganization();

  if (isLoading) {
    return (
      <div className="flex flex-col items-center justify-center min-h-[60vh] gap-3 text-slate-400">
        <Loader2 className="h-6 w-6 animate-spin text-blue-500" />
        <p className="text-sm">Loading workspace…</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-8 max-w-lg mx-auto mt-12">
        <div className="bg-slate-950 border border-red-900/40 rounded-xl p-6 text-center space-y-4">
          <div className="h-10 w-10 rounded-full bg-red-950/40 border border-red-800/50 flex items-center justify-center mx-auto text-red-400">
            <AlertCircle className="h-5 w-5" />
          </div>
          <div>
            <h2 className="text-lg font-semibold text-slate-100">Unable to load workspace</h2>
            <p className="text-sm text-slate-400 mt-1">{error}</p>
          </div>
          <button
            type="button"
            onClick={() => refetchOrganizations()}
            className="inline-flex items-center justify-center px-4 py-2 bg-slate-800 hover:bg-slate-700 text-slate-200 text-sm font-medium rounded-lg transition-colors"
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  if (organizations.length === 0) {
    return <OrgOnboarding />;
  }

  return <>{children}</>;
}
