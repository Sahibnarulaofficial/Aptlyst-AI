'use client';

import React from 'react';
import { Building2, ShieldCheck, ChevronDown } from 'lucide-react';
import { useOrganization } from '@/lib/organization-context';

export function OrgSwitcher() {
  const { organizations, currentOrg, setCurrentOrgId } = useOrganization();

  if (!currentOrg) return null;

  return (
    <div className="px-3 py-2 border-b border-slate-800 bg-slate-900/40">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <div className="h-6 w-6 rounded bg-slate-800 border border-slate-700 flex items-center justify-center shrink-0">
            <Building2 className="h-3.5 w-3.5 text-slate-300" />
          </div>
          <span className="text-sm font-semibold text-slate-200 truncate">
            {currentOrg.name}
          </span>
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium uppercase tracking-wider bg-blue-950/60 text-blue-400 border border-blue-800/50">
            <ShieldCheck className="h-2.5 w-2.5" />
            {currentOrg.role}
          </span>
        </div>
      </div>

      {organizations.length > 1 && (
        <div className="mt-2">
          <label htmlFor="org-select" className="sr-only">
            Switch organization
          </label>
          <div className="relative">
            <select
              id="org-select"
              value={currentOrg.id}
              onChange={(e) => setCurrentOrgId(e.target.value)}
              className="w-full appearance-none bg-slate-950 border border-slate-700 hover:border-slate-600 rounded px-2.5 py-1 text-xs text-slate-200 focus:outline-none focus:ring-1 focus:ring-blue-500 pr-6 cursor-pointer"
            >
              {organizations.map((org) => (
                <option key={org.id} value={org.id}>
                  {org.name} ({org.role})
                </option>
              ))}
            </select>
            <ChevronDown className="absolute right-2 top-1/2 -translate-y-1/2 h-3 w-3 text-slate-400 pointer-events-none" />
          </div>
        </div>
      )}
    </div>
  );
}
