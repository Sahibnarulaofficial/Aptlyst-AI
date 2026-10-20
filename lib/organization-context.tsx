'use client';

import React, { createContext, useContext, useEffect, useState, useCallback } from 'react';
import type { Organization, OrganizationWithRole } from '@/lib/types';
import { listOrganizations, setupOrganization, ApiError } from '@/lib/api';

export interface OrganizationContextType {
  organizations: OrganizationWithRole[];
  currentOrg: OrganizationWithRole | null;
  setCurrentOrgId: (id: string) => void;
  isLoading: boolean;
  error: string | null;
  refetchOrganizations: () => Promise<OrganizationWithRole[]>;
  createOrganization: (name: string) => Promise<Organization>;
  isCreating: boolean;
  createError: string | null;
  clearCreateError: () => void;
}

const OrganizationContext = createContext<OrganizationContextType | undefined>(undefined);

export function OrganizationProvider({ children }: { children: React.ReactNode }) {
  const [organizations, setOrganizations] = useState<OrganizationWithRole[]>([]);
  const [currentOrgId, setCurrentOrgId] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const [isCreating, setIsCreating] = useState<boolean>(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const fetchOrgs = useCallback(async (): Promise<OrganizationWithRole[]> => {
    try {
      setIsLoading(true);
      setError(null);
      const orgs = await listOrganizations();
      setOrganizations(orgs);

      // Establish currentOrg if available and not yet set or no longer valid
      setCurrentOrgId((prevId) => {
        if (prevId && orgs.some((o) => o.id === prevId)) {
          return prevId;
        }
        return orgs.length > 0 ? orgs[0].id : null;
      });

      return orgs;
    } catch (err: unknown) {
      const message =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : 'Failed to load organizations';
      setError(message);
      return [];
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    let ignore = false;

    async function load() {
      try {
        setError(null);
        const orgs = await listOrganizations();
        if (ignore) return;
        setOrganizations(orgs);
        setCurrentOrgId((prevId) => {
          if (prevId && orgs.some((o) => o.id === prevId)) {
            return prevId;
          }
          return orgs.length > 0 ? orgs[0].id : null;
        });
      } catch (err: unknown) {
        if (ignore) return;
        const message =
          err instanceof ApiError
            ? err.message
            : err instanceof Error
              ? err.message
              : 'Failed to load organizations';
        setError(message);
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
  }, []);

  const createOrg = useCallback(
    async (name: string): Promise<Organization> => {
      if (isCreating) {
        throw new Error('An organization creation request is already in progress');
      }
      setIsCreating(true);
      setCreateError(null);

      try {
        const created = await setupOrganization(name);
        // Refresh organization list and establish active organization from newly created org
        await fetchOrgs();
        if (created?.id) {
          setCurrentOrgId(created.id);
        }
        return created;
      } catch (err: unknown) {
        if (err instanceof ApiError && err.status === 409) {
          // 409 Conflict: user already owns an org (e.g. concurrent onboarding).
          // Refetch to sync state; only set error if no org was found.
          const refreshed = await fetchOrgs();
          if (refreshed.length === 0) {
            setCreateError(err.message);
          }
        } else {
          const message =
            err instanceof ApiError
              ? err.message
              : err instanceof Error
                ? err.message
                : 'Failed to create organization';
          setCreateError(message);
        }
        throw err;
      } finally {
        setIsCreating(false);
      }
    },
    [isCreating, fetchOrgs],
  );

  const clearCreateError = useCallback(() => {
    setCreateError(null);
  }, []);

  const currentOrg = organizations.find((o) => o.id === currentOrgId) ?? null;

  return (
    <OrganizationContext.Provider
      value={{
        organizations,
        currentOrg,
        setCurrentOrgId,
        isLoading,
        error,
        refetchOrganizations: fetchOrgs,
        createOrganization: createOrg,
        isCreating,
        createError,
        clearCreateError,
      }}
    >
      {children}
    </OrganizationContext.Provider>
  );
}

export function useOrganization() {
  const context = useContext(OrganizationContext);
  if (!context) {
    throw new Error('useOrganization must be used within an OrganizationProvider');
  }
  return context;
}
