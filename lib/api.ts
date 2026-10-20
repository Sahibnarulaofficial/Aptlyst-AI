import type { Organization, OrganizationWithRole } from '@/lib/types';

/**
 * Frontend-to-backend API client for Aptlyst AI.
 * Handles forwarding authenticated Supabase sessions to the FastAPI backend.
 */

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

export class ApiError extends Error {
  status: number;
  data?: unknown;

  constructor(status: number, message: string, data?: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.data = data;
  }
}

export interface FetchOptions extends Omit<RequestInit, 'headers'> {
  token?: string;
  headers?: Record<string, string> | Headers;
}

/**
 * Perform an authenticated request to the FastAPI backend.
 * If token is omitted, it attempts to resolve the session token from Supabase.
 */
export async function apiFetch<T>(
  endpoint: string,
  options: FetchOptions = {},
): Promise<T> {
  const { token, headers: customHeaders, ...fetchInit } = options;

  let accessToken = token;

  if (!accessToken) {
    if (typeof window !== 'undefined') {
      // Browser environment: dynamically import client to avoid SSR issues
      const { createClient } = await import('@/lib/supabase/client');
      const supabase = createClient();
      const { data } = await supabase.auth.getSession();
      accessToken = data.session?.access_token;
    } else {
      // Server environment
      const { createClient } = await import('@/lib/supabase/server');
      const supabase = await createClient();
      const { data } = await supabase.auth.getSession();
      accessToken = data.session?.access_token;
    }
  }

  const headers = new Headers(customHeaders);
  if (accessToken) {
    headers.set('Authorization', `Bearer ${accessToken}`);
  }
  if (!headers.has('Content-Type') && !(fetchInit.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json');
  }

  const cleanBase = API_BASE_URL.replace(/\/$/, '');
  const cleanEndpoint = endpoint.startsWith('/') ? endpoint : `/${endpoint}`;
  const url = `${cleanBase}${cleanEndpoint}`;

  const response = await fetch(url, {
    ...fetchInit,
    headers,
  });

  if (!response.ok) {
    let errorDetail = `Request failed with status ${response.status}`;
    let errorData: unknown = undefined;
    try {
      errorData = await response.json();
      if (
        typeof errorData === 'object' &&
        errorData !== null &&
        'detail' in errorData
      ) {
        errorDetail = String((errorData as { detail: unknown }).detail);
      }
    } catch {
      // Response was not JSON
    }
    throw new ApiError(response.status, errorDetail, errorData);
  }

  if (response.status === 204) {
    return undefined as unknown as T;
  }

  return response.json() as Promise<T>;
}

// ─── Organization API Methods ────────────────────────────────────────────────

export async function listOrganizations(token?: string): Promise<OrganizationWithRole[]> {
  return apiFetch<OrganizationWithRole[]>('/api/v1/organizations', {
    method: 'GET',
    token,
  });
}

export async function setupOrganization(
  name: string,
  token?: string,
): Promise<Organization> {
  return apiFetch<Organization>('/api/v1/organizations/setup', {
    method: 'POST',
    body: JSON.stringify({ name }),
    token,
  });
}

export async function getOrganization(
  orgId: string,
  token?: string,
): Promise<Organization> {
  return apiFetch<Organization>(`/api/v1/organizations/${orgId}`, {
    method: 'GET',
    token,
  });
}

export async function updateOrganization(
  orgId: string,
  name: string,
  token?: string,
): Promise<Organization> {
  return apiFetch<Organization>(`/api/v1/organizations/${orgId}`, {
    method: 'PATCH',
    body: JSON.stringify({ name }),
    token,
  });
}
