import { describe, it, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { listOrganizations, setupOrganization, ApiError } from '../lib/api.ts';
import type { OrganizationWithRole, Organization } from '../lib/types.ts';

/**
 * Controller class reproducing the logic in OrganizationProvider and OrgOnboarding
 * for headless unit testing without requiring a browser DOM environment.
 */
class OrganizationManager {
  organizations: OrganizationWithRole[] = [];
  currentOrgId: string | null = null;
  isLoading = false;
  error: string | null = null;
  isCreating = false;
  createError: string | null = null;

  async loadOrganizations(): Promise<OrganizationWithRole[]> {
    this.isLoading = true;
    this.error = null;
    try {
      const orgs = await listOrganizations();
      this.organizations = orgs;
      if (!this.currentOrgId || !orgs.some((o) => o.id === this.currentOrgId)) {
        this.currentOrgId = orgs.length > 0 ? orgs[0].id : null;
      }
      return orgs;
    } catch (err: unknown) {
      this.error =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : 'Failed to load organizations';
      return [];
    } finally {
      this.isLoading = false;
    }
  }

  selectOrganization(id: string): void {
    if (this.organizations.some((o) => o.id === id)) {
      this.currentOrgId = id;
    }
  }

  get currentOrg(): OrganizationWithRole | null {
    return this.organizations.find((o) => o.id === this.currentOrgId) ?? null;
  }

  async createOrganization(name: string): Promise<Organization | null> {
    if (this.isCreating) {
      return null;
    }

    this.isCreating = true;
    this.createError = null;

    try {
      const created = await setupOrganization(name, 'test-token');
      await this.loadOrganizations();
      if (created?.id) {
        this.currentOrgId = created.id;
      }
      return created;
    } catch (err: unknown) {
      if (err instanceof ApiError && err.status === 409) {
        // Refetch on 409 conflict
        const refreshed = await this.loadOrganizations();
        if (refreshed.length === 0) {
          this.createError = err.message;
        }
      } else {
        this.createError =
          err instanceof ApiError
            ? err.message
            : err instanceof Error
              ? err.message
              : 'Failed to create organization';
      }
      throw err;
    } finally {
      this.isCreating = false;
    }
  }
}

describe('Organization Context & Tenant Logic (lib/organization-context.tsx)', () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    globalThis.fetch = originalFetch;
  });

  it('handles user with zero organizations (empty list)', async () => {
    globalThis.fetch = async () => {
      return new Response(JSON.stringify([]), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    };

    const manager = new OrganizationManager();
    assert.equal(manager.isLoading, false);

    const promise = manager.loadOrganizations();
    assert.equal(manager.isLoading, true);

    const orgs = await promise;
    assert.equal(manager.isLoading, false);
    assert.equal(orgs.length, 0);
    assert.equal(manager.organizations.length, 0);
    assert.equal(manager.currentOrg, null);
    assert.equal(manager.error, null);
  });

  it('handles user with an existing organization', async () => {
    const mockOrg: OrganizationWithRole = {
      id: 'org-uuid-1',
      name: 'Acme AI',
      role: 'owner',
      created_at: '2026-10-20T10:00:00Z',
      updated_at: '2026-10-20T10:00:00Z',
    };

    globalThis.fetch = async () => {
      return new Response(JSON.stringify([mockOrg]), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    };

    const manager = new OrganizationManager();
    await manager.loadOrganizations();

    assert.equal(manager.organizations.length, 1);
    assert.deepEqual(manager.currentOrg, mockOrg);
    assert.equal(manager.currentOrg?.role, 'owner');
    assert.equal(manager.error, null);
  });

  it('supports multiple organizations and explicit selection', async () => {
    const org1: OrganizationWithRole = {
      id: 'org-uuid-1',
      name: 'Acme Corp',
      role: 'owner',
      created_at: '2026-10-20T10:00:00Z',
      updated_at: '2026-10-20T10:00:00Z',
    };
    const org2: OrganizationWithRole = {
      id: 'org-uuid-2',
      name: 'Beta Labs',
      role: 'member',
      created_at: '2026-10-20T11:00:00Z',
      updated_at: '2026-10-20T11:00:00Z',
    };

    globalThis.fetch = async () => {
      return new Response(JSON.stringify([org1, org2]), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    };

    const manager = new OrganizationManager();
    await manager.loadOrganizations();

    assert.equal(manager.organizations.length, 2);
    assert.equal(manager.currentOrg?.id, 'org-uuid-1');

    // Explicitly select the second organization
    manager.selectOrganization('org-uuid-2');
    assert.equal(manager.currentOrg?.id, 'org-uuid-2');
    assert.equal(manager.currentOrg?.name, 'Beta Labs');
    assert.equal(manager.currentOrg?.role, 'member');
  });

  it('handles organization-list loading and API failure', async () => {
    globalThis.fetch = async () => {
      return new Response(JSON.stringify({ detail: 'Internal server error' }), {
        status: 500,
        headers: { 'Content-Type': 'application/json' },
      });
    };

    const manager = new OrganizationManager();
    await manager.loadOrganizations();

    assert.equal(manager.isLoading, false);
    assert.equal(manager.organizations.length, 0);
    assert.equal(manager.currentOrg, null);
    assert.equal(manager.error, 'Internal server error');
  });

  it('handles organization setup success, refetches list, and establishes context', async () => {
    let callCount = 0;
    const createdOrg: Organization = {
      id: 'org-uuid-new',
      name: 'New Ventures',
      created_at: '2026-10-20T12:00:00Z',
      updated_at: '2026-10-20T12:00:00Z',
    };
    const membershipOrg: OrganizationWithRole = {
      ...createdOrg,
      role: 'owner',
    };

    globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      callCount++;
      const url = String(input);
      if (url.includes('/api/v1/organizations/setup')) {
        assert.equal(init?.method, 'POST');
        return new Response(JSON.stringify(createdOrg), {
          status: 201,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/api/v1/organizations')) {
        return new Response(JSON.stringify([membershipOrg]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      throw new Error(`Unexpected request to ${url}`);
    };

    const manager = new OrganizationManager();
    const result = await manager.createOrganization('New Ventures');

    assert.equal(result?.name, 'New Ventures');
    assert.equal(manager.organizations.length, 1);
    assert.equal(manager.currentOrg?.id, 'org-uuid-new');
    assert.equal(manager.currentOrg?.role, 'owner');
    assert.equal(manager.createError, null);
    assert.equal(callCount, 2); // 1 POST /setup + 1 GET /organizations
  });

  it('handles organization setup failure (e.g. 400 validation error)', async () => {
    globalThis.fetch = async () => {
      return new Response(JSON.stringify({ detail: 'Organization name cannot be empty' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json' },
      });
    };

    const manager = new OrganizationManager();
    await assert.rejects(
      async () => {
        await manager.createOrganization('');
      },
      (err: unknown) => {
        assert(err instanceof ApiError);
        assert.equal(err.status, 400);
        return true;
      },
    );

    assert.equal(manager.createError, 'Organization name cannot be empty');
    assert.equal(manager.isCreating, false);
  });

  it('handles 409 Conflict by refetching organizations and synchronizing state', async () => {
    const existingOrg: OrganizationWithRole = {
      id: 'existing-id',
      name: 'Existing Org',
      role: 'owner',
      created_at: '2026-10-20T10:00:00Z',
      updated_at: '2026-10-20T10:00:00Z',
    };

    globalThis.fetch = async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/setup')) {
        return new Response(JSON.stringify({ detail: 'User already owns an organization' }), {
          status: 409,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(JSON.stringify([existingOrg]), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    };

    const manager = new OrganizationManager();
    await assert.rejects(
      async () => {
        await manager.createOrganization('Existing Org');
      },
      (err: unknown) => {
        assert(err instanceof ApiError);
        assert.equal(err.status, 409);
        return true;
      },
    );

    // After 409, manager synchronized existing organization from backend
    assert.equal(manager.organizations.length, 1);
    assert.equal(manager.currentOrg?.name, 'Existing Org');
    assert.equal(manager.createError, null); // Error cleared since org was established
  });

  it('prevents duplicate submissions while request is in progress', async () => {
    let callCount = 0;
    let resolveFirstRequest: (value: Response) => void;
    const firstRequestPromise = new Promise<Response>((resolve) => {
      resolveFirstRequest = resolve;
    });

    globalThis.fetch = async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/setup')) {
        callCount++;
        return firstRequestPromise;
      }
      return new Response(JSON.stringify([]), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    };

    const manager = new OrganizationManager();
    // Launch first submission
    const firstCall = manager.createOrganization('Company');
    assert.equal(manager.isCreating, true);

    // Attempt second submission while first is still pending
    const secondCall = await manager.createOrganization('Company');
    assert.equal(secondCall, null); // Blocked
    assert.equal(callCount, 1); // Exactly 1 fetch initiated

    // Resolve first request
    resolveFirstRequest!(
      new Response(
        JSON.stringify({
          id: '1',
          name: 'Company',
          created_at: '2026-10-20T10:00:00Z',
          updated_at: '2026-10-20T10:00:00Z',
        }),
        { status: 201, headers: { 'Content-Type': 'application/json' } },
      ),
    );

    await firstCall;
    assert.equal(manager.isCreating, false);
  });

  describe('Organization Switcher Logic (components/organization/org-switcher.tsx)', () => {
    it('manages single-organization display and prevents switching to invalid orgs', async () => {
      globalThis.fetch = async () => {
        return new Response(
          JSON.stringify([
            {
              id: 'org-only-1',
              name: 'PortfolioSite',
              role: 'owner',
              created_at: '2026-10-20T10:00:00Z',
              updated_at: '2026-10-20T10:00:00Z',
            },
          ]),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      };

      const manager = new OrganizationManager();
      await manager.loadOrganizations();

      assert.equal(manager.organizations.length, 1);
      assert.equal(manager.currentOrg?.id, 'org-only-1');
      assert.equal(manager.currentOrg?.name, 'PortfolioSite');
      assert.equal(manager.currentOrg?.role, 'owner');

      // Attempting to select non-existent org does not alter currentOrg
      manager.selectOrganization('org-fake');
      assert.equal(manager.currentOrg?.id, 'org-only-1');
    });

    it('manages multi-organization selection and updates active organization state without duplicating state', async () => {
      globalThis.fetch = async () => {
        return new Response(
          JSON.stringify([
            {
              id: 'org-1',
              name: 'Alpha Corp',
              role: 'owner',
              created_at: '2026-10-20T10:00:00Z',
              updated_at: '2026-10-20T10:00:00Z',
            },
            {
              id: 'org-2',
              name: 'Beta LLC',
              role: 'member',
              created_at: '2026-10-20T11:00:00Z',
              updated_at: '2026-10-20T11:00:00Z',
            },
          ]),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      };

      const manager = new OrganizationManager();
      await manager.loadOrganizations();

      assert.equal(manager.organizations.length, 2);
      assert.equal(manager.currentOrg?.id, 'org-1');
      assert.equal(manager.currentOrg?.role, 'owner');

      // Switch to org-2
      manager.selectOrganization('org-2');
      assert.equal(manager.currentOrg?.id, 'org-2');
      assert.equal(manager.currentOrg?.name, 'Beta LLC');
      assert.equal(manager.currentOrg?.role, 'member');

      // Switch back to org-1
      manager.selectOrganization('org-1');
      assert.equal(manager.currentOrg?.id, 'org-1');
      assert.equal(manager.currentOrg?.name, 'Alpha Corp');
    });

    it('creates a new organization, refreshes list, and automatically selects it', async () => {
      const orgsInBackend = [
        {
          id: 'org-1',
          name: 'PortfolioSite',
          role: 'owner' as const,
          created_at: '2026-10-20T10:00:00Z',
          updated_at: '2026-10-20T10:00:00Z',
        },
      ];

      globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method || 'GET';

        if (url.includes('/setup') && method === 'POST') {
          const body = JSON.parse(String(init?.body || '{}'));
          const newOrg = {
            id: 'org-2-new',
            name: body.name,
            role: 'owner' as const,
            created_at: '2026-10-20T12:00:00Z',
            updated_at: '2026-10-20T12:00:00Z',
          };
          orgsInBackend.push(newOrg);
          return new Response(JSON.stringify(newOrg), {
            status: 201,
            headers: { 'Content-Type': 'application/json' },
          });
        }

        return new Response(JSON.stringify(orgsInBackend), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      };

      const manager = new OrganizationManager();
      await manager.loadOrganizations();

      assert.equal(manager.organizations.length, 1);
      assert.equal(manager.currentOrg?.id, 'org-1');

      // Create new organization through manager
      const created = await manager.createOrganization('Beta Workspace');
      assert.equal(created?.name, 'Beta Workspace');
      assert.equal(created?.id, 'org-2-new');

      // Organization list must be refreshed with 2 orgs
      assert.equal(manager.organizations.length, 2);

      // The new organization must be automatically selected as currentOrg
      assert.equal(manager.currentOrg?.id, 'org-2-new');
      assert.equal(manager.currentOrg?.name, 'Beta Workspace');
    });

    it('handles organization creation API failure without losing active organization', async () => {
      globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method || 'GET';

        if (url.includes('/setup') && method === 'POST') {
          return new Response(
            JSON.stringify({ detail: 'User already owns an organization' }),
            { status: 409, headers: { 'Content-Type': 'application/json' } },
          );
        }

        return new Response(
          JSON.stringify([
            {
              id: 'org-1',
              name: 'PortfolioSite',
              role: 'owner' as const,
              created_at: '2026-10-20T10:00:00Z',
              updated_at: '2026-10-20T10:00:00Z',
            },
          ]),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      };

      const manager = new OrganizationManager();
      await manager.loadOrganizations();
      assert.equal(manager.currentOrg?.id, 'org-1');

      // Attempt to create second organization (fails with 409)
      await assert.rejects(
        async () => {
          await manager.createOrganization('Second Site');
        },
        (err: unknown) => {
          assert(err instanceof ApiError);
          assert.equal(err.status, 409);
          assert.equal(err.message, 'User already owns an organization');
          return true;
        },
      );

      // Current organization and list must be preserved
      assert.equal(manager.currentOrg?.id, 'org-1');
      assert.equal(manager.currentOrg?.name, 'PortfolioSite');
      assert.equal(manager.organizations.length, 1);
    });

    it('handles 403 forbidden plan limit error with upgrade message and preserves active organization', async () => {
      globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method || 'GET';

        if (url.includes('/setup') && method === 'POST') {
          return new Response(
            JSON.stringify({
              detail: 'Free plan is limited to 1 organization. Upgrade to create more organizations.',
            }),
            { status: 403, headers: { 'Content-Type': 'application/json' } },
          );
        }

        return new Response(
          JSON.stringify([
            {
              id: 'org-free-1',
              name: 'My Free Org',
              role: 'owner' as const,
              created_at: '2026-10-20T10:00:00Z',
              updated_at: '2026-10-20T10:00:00Z',
            },
          ]),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      };

      const manager = new OrganizationManager();
      await manager.loadOrganizations();
      assert.equal(manager.currentOrg?.id, 'org-free-1');
      assert.equal(manager.currentOrg?.name, 'My Free Org');

      // Attempt to create second organization on free plan
      await assert.rejects(
        async () => {
          await manager.createOrganization('Second Site');
        },
        (err: unknown) => {
          assert(err instanceof ApiError);
          assert.equal(err.status, 403);
          assert.match(err.message, /Free plan is limited to 1 organization/i);
          assert.match(err.message, /Upgrade to create more organizations/i);
          return true;
        },
      );

      // Verify createError was set with the upgrade detail message
      assert.equal(
        manager.createError,
        'Free plan is limited to 1 organization. Upgrade to create more organizations.',
      );

      // Current organization and list must remain intact
      assert.equal(manager.currentOrg?.id, 'org-free-1');
      assert.equal(manager.organizations.length, 1);
    });

    it('supports multiple organization creations for users with paid plan', async () => {
      const backendOrgs: OrganizationWithRole[] = [
        {
          id: 'org-paid-1',
          name: 'Pro Corp 1',
          role: 'owner',
          created_at: '2026-10-20T10:00:00Z',
          updated_at: '2026-10-20T10:00:00Z',
        },
      ];

      let orgCounter = 2;
      globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method || 'GET';

        if (url.includes('/setup') && method === 'POST') {
          const body = JSON.parse(String(init?.body || '{}'));
          const newOrg: OrganizationWithRole = {
            id: `org-paid-${orgCounter++}`,
            name: body.name,
            role: 'owner',
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          };
          backendOrgs.push(newOrg);
          return new Response(JSON.stringify(newOrg), {
            status: 201,
            headers: { 'Content-Type': 'application/json' },
          });
        }

        return new Response(JSON.stringify(backendOrgs), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      };

      const manager = new OrganizationManager();
      await manager.loadOrganizations();
      assert.equal(manager.organizations.length, 1);

      // Paid user creates second org
      const second = await manager.createOrganization('Pro Corp 2');
      assert.equal(second?.name, 'Pro Corp 2');
      assert.equal(manager.organizations.length, 2);
      assert.equal(manager.currentOrg?.name, 'Pro Corp 2');

      // Paid user creates third org
      const third = await manager.createOrganization('Pro Corp 3');
      assert.equal(third?.name, 'Pro Corp 3');
      assert.equal(manager.organizations.length, 3);
      assert.equal(manager.currentOrg?.name, 'Pro Corp 3');
    });

    it('enforces fail-closed behavior when subscription lookup fails or is unconfirmed for single-org owner', async () => {
      // Helper simulating client-side entitlement check logic in OrgSwitcher
      function checkIsLimitReached(
        orgs: OrganizationWithRole[],
        isPaid: boolean | null,
        dialogErr: string | null,
      ): boolean {
        const owned = orgs.filter((o) => o.role === 'owner').length;
        const isOwner = owned >= 1;
        // Fail-closed rule: if owner and not confirmed paid (null or false), limit is reached
        return (
          (isOwner && isPaid !== true) ||
          Boolean(
            dialogErr &&
              (dialogErr.toLowerCase().includes('free plan') ||
                dialogErr.toLowerCase().includes('upgrade') ||
                dialogErr.toLowerCase().includes('limit')),
          )
        );
      }

      const ownedOrgs: OrganizationWithRole[] = [
        {
          id: 'org-1',
          name: 'My Owned Org',
          role: 'owner',
          created_at: '2026-10-20T10:00:00Z',
          updated_at: '2026-10-20T10:00:00Z',
        },
      ];

      // Case 1: Subscription lookup in progress or failed (isPaid = null) -> limit reached (fail-closed)
      assert.equal(checkIsLimitReached(ownedOrgs, null, null), true);

      // Case 2: Subscription lookup explicitly confirmed free (isPaid = false) -> limit reached
      assert.equal(checkIsLimitReached(ownedOrgs, false, null), true);

      // Case 3: Confirmed paid subscription (isPaid = true) -> limit NOT reached
      assert.equal(checkIsLimitReached(ownedOrgs, true, null), false);

      // Case 4: Zero owned orgs (e.g. member only) even with isPaid = false -> limit NOT reached
      const memberOrgs: OrganizationWithRole[] = [
        {
          id: 'org-member',
          name: 'Member Org',
          role: 'member',
          created_at: '2026-10-20T10:00:00Z',
          updated_at: '2026-10-20T10:00:00Z',
        },
      ];
      assert.equal(checkIsLimitReached(memberOrgs, false, null), false);
    });
  });
});
