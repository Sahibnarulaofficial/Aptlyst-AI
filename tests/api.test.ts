import { describe, it, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { apiFetch, ApiError, listOrganizations, setupOrganization } from '../lib/api.ts';

describe('Frontend API Client (lib/api.ts)', () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    globalThis.fetch = originalFetch;
  });

  it('injects Authorization Bearer header when token is provided', async () => {
    let capturedUrl = '';
    let capturedHeaders: Headers | undefined;

    globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      capturedUrl = String(input);
      capturedHeaders = new Headers(init?.headers);
      return new Response(JSON.stringify([{ id: '123', name: 'Org 1', role: 'owner' }]), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    };

    const res = await apiFetch<unknown[]>('/api/v1/organizations', {
      token: 'fake-jwt-token',
    });

    assert.equal(capturedUrl, 'http://localhost:8000/api/v1/organizations');
    assert.equal(capturedHeaders?.get('Authorization'), 'Bearer fake-jwt-token');
    assert.equal(capturedHeaders?.get('Content-Type'), 'application/json');
    assert.equal((res as unknown[]).length, 1);
  });

  it('throws ApiError with detail message on non-2xx responses', async () => {
    globalThis.fetch = async () => {
      return new Response(JSON.stringify({ detail: 'Token has expired' }), {
        status: 401,
        headers: { 'Content-Type': 'application/json' },
      });
    };

    await assert.rejects(
      async () => {
        await apiFetch('/api/v1/organizations', { token: 'expired-token' });
      },
      (err: unknown) => {
        assert(err instanceof ApiError);
        assert.equal(err.status, 401);
        assert.equal(err.message, 'Token has expired');
        return true;
      },
    );
  });

  it('formats POST request body as JSON in setupOrganization', async () => {
    let capturedMethod = '';
    let capturedBody = '';

    globalThis.fetch = async (_input: RequestInfo | URL, init?: RequestInit) => {
      capturedMethod = init?.method || '';
      capturedBody = String(init?.body || '');
      return new Response(JSON.stringify({ id: 'org-1', name: 'Acme Corp' }), {
        status: 201,
        headers: { 'Content-Type': 'application/json' },
      });
    };

    const org = await setupOrganization('Acme Corp', 'my-token');

    assert.equal(capturedMethod, 'POST');
    assert.equal(capturedBody, JSON.stringify({ name: 'Acme Corp' }));
    assert.equal(org.name, 'Acme Corp');
  });

  it('handles 204 No Content appropriately without throwing JSON parse error', async () => {
    globalThis.fetch = async () => {
      return new Response(null, { status: 204 });
    };

    const res = await apiFetch('/api/v1/something', { token: 'token' });
    assert.equal(res, undefined);
  });

  it('calls listOrganizations with GET method and Bearer token', async () => {
    let capturedMethod = '';
    let capturedAuth = '';

    globalThis.fetch = async (_input: RequestInfo | URL, init?: RequestInit) => {
      capturedMethod = init?.method || '';
      capturedAuth = new Headers(init?.headers).get('Authorization') || '';
      return new Response(
        JSON.stringify([
          {
            id: 'org-1',
            name: 'Acme',
            role: 'owner',
            created_at: '2026-10-20T00:00:00Z',
            updated_at: '2026-10-20T00:00:00Z',
          },
        ]),
        {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        },
      );
    };

    const orgs = await listOrganizations('test-token');
    assert.equal(capturedMethod, 'GET');
    assert.equal(capturedAuth, 'Bearer test-token');
    assert.equal(orgs.length, 1);
    assert.equal(orgs[0].name, 'Acme');
    assert.equal(orgs[0].role, 'owner');
  });
});
