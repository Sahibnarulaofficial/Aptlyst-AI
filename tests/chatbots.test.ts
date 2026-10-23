import { describe, it, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  listChatbots,
  createChatbot,
  getChatbot,
  updateChatbot,
  ApiError,
} from '../lib/api.ts';
import type { Chatbot, ChatbotCreateInput, ChatbotUpdateInput, ChatbotStatus } from '../lib/types.ts';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

/**
 * Controller classes reproducing the state management logic from the React pages
 * for headless unit testing in Node.js test environment.
 */

class ChatbotListController {
  chatbots: Chatbot[] = [];
  isLoading = false;
  error: string | null = null;
  activeOrgId: string | null = null;

  async setOrganization(orgId: string | null): Promise<void> {
    // Clear previous organization's data immediately to prevent stale cross-tenant leakage
    this.chatbots = [];
    this.error = null;
    this.activeOrgId = orgId;

    if (!orgId) {
      this.isLoading = false;
      return;
    }

    this.isLoading = true;
    const requestedOrgId = orgId;

    try {
      const data = await listChatbots(requestedOrgId);
      // Guard against race conditions if organization changed while request was in-flight
      if (this.activeOrgId === requestedOrgId) {
        this.chatbots = data;
      }
    } catch (err: unknown) {
      if (this.activeOrgId === requestedOrgId) {
        this.error =
          err instanceof ApiError
            ? err.message
            : err instanceof Error
              ? err.message
              : 'Failed to load chatbots';
      }
    } finally {
      if (this.activeOrgId === requestedOrgId) {
        this.isLoading = false;
      }
    }
  }

  async retry(): Promise<void> {
    if (this.activeOrgId) {
      await this.setOrganization(this.activeOrgId);
    }
  }
}

class ChatbotCreateController {
  isSubmitting = false;
  error: string | null = null;

  async submit(
    orgId: string | null,
    formData: {
      name: string;
      websiteUrl?: string;
      description?: string;
      welcomeMessage?: string;
      instructions?: string;
      primaryColor?: string;
    },
  ): Promise<Chatbot | null> {
    if (this.isSubmitting) return null;

    if (!orgId) {
      this.error = 'No active organization selected. Please select or create an organization first.';
      return null;
    }

    const trimmedName = formData.name.trim();
    if (!trimmedName) {
      this.error = 'Chatbot name is required.';
      return null;
    }

    const trimmedUrl = formData.websiteUrl?.trim();
    if (trimmedUrl && !trimmedUrl.startsWith('http://') && !trimmedUrl.startsWith('https://')) {
      this.error = 'Website URL must start with http:// or https://';
      return null;
    }

    this.isSubmitting = true;
    this.error = null;

    const payload: ChatbotCreateInput = {
      name: trimmedName,
      description: formData.description?.trim() || null,
      website_url: trimmedUrl || null,
      welcome_message: formData.welcomeMessage?.trim() || null,
      instructions: formData.instructions?.trim() || null,
      primary_color: formData.primaryColor || '#3b82f6',
      status: 'draft',
    };

    try {
      const created = await createChatbot(orgId, payload);
      return created;
    } catch (err: unknown) {
      this.error =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : 'Failed to create chatbot';
      return null;
    } finally {
      this.isSubmitting = false;
    }
  }
}

class ChatbotDetailController {
  bot: Chatbot | null = null;
  isLoading = false;
  error: string | null = null;
  isNotFound = false;
  activeOrgId: string | null = null;
  activeBotId: string | null = null;

  formData = {
    name: '',
    status: 'draft' as ChatbotStatus,
    description: '',
    websiteUrl: '',
    welcomeMessage: '',
    instructions: '',
    primaryColor: '#3b82f6',
  };
  isSaving = false;
  saveError: string | null = null;
  saveSuccess = false;

  async loadDetail(orgId: string | null, botId: string | null): Promise<void> {
    // Clear previous chatbot state immediately
    this.bot = null;
    this.error = null;
    this.isNotFound = false;
    this.saveError = null;
    this.saveSuccess = false;
    this.activeOrgId = orgId;
    this.activeBotId = botId;

    if (!orgId || !botId) {
      this.isLoading = false;
      return;
    }

    this.isLoading = true;
    const currentRequestOrg = orgId;
    const currentRequestBot = botId;

    try {
      const data = await getChatbot(currentRequestOrg, currentRequestBot);
      if (this.activeOrgId === currentRequestOrg && this.activeBotId === currentRequestBot) {
        this.bot = data;
        this.formData = {
          name: data.name,
          status: data.status,
          description: data.description || '',
          websiteUrl: data.website_url || '',
          welcomeMessage: data.welcome_message || '',
          instructions: data.instructions || '',
          primaryColor: data.primary_color || '#3b82f6',
        };
      }
    } catch (err: unknown) {
      if (this.activeOrgId === currentRequestOrg && this.activeBotId === currentRequestBot) {
        if (err instanceof ApiError && err.status === 404) {
          this.isNotFound = true;
        } else {
          this.error =
            err instanceof ApiError
              ? err.message
              : err instanceof Error
                ? err.message
                : 'Failed to load chatbot';
        }
      }
    } finally {
      if (this.activeOrgId === currentRequestOrg && this.activeBotId === currentRequestBot) {
        this.isLoading = false;
      }
    }
  }

  async save(
    orgId: string | null,
    botId: string | null,
    input: {
      name: string;
      status?: ChatbotStatus;
      description?: string;
      websiteUrl?: string;
      welcomeMessage?: string;
      instructions?: string;
      primaryColor?: string;
    },
  ): Promise<Chatbot | null> {
    if (this.isSaving) return null;
    if (!orgId || !botId) {
      this.saveError = 'No active organization or chatbot';
      this.saveSuccess = false;
      return null;
    }

    const trimmedName = input.name.trim();
    if (!trimmedName) {
      this.saveError = 'Chatbot name is required.';
      this.saveSuccess = false;
      return null;
    }

    const trimmedUrl = input.websiteUrl?.trim();
    if (trimmedUrl && !trimmedUrl.startsWith('http://') && !trimmedUrl.startsWith('https://')) {
      this.saveError = 'Website URL must start with http:// or https://';
      this.saveSuccess = false;
      return null;
    }

    this.isSaving = true;
    this.saveError = null;
    this.saveSuccess = false;

    const payload: ChatbotUpdateInput = {
      name: trimmedName,
      status: input.status,
      description: input.description?.trim() || null,
      website_url: trimmedUrl || null,
      welcome_message: input.welcomeMessage?.trim() || null,
      instructions: input.instructions?.trim() || null,
      primary_color: input.primaryColor || '#3b82f6',
    };

    try {
      const updated = await updateChatbot(orgId, botId, payload);
      this.bot = updated;
      this.formData = {
        name: updated.name,
        status: updated.status,
        description: updated.description || '',
        websiteUrl: updated.website_url || '',
        welcomeMessage: updated.welcome_message || '',
        instructions: updated.instructions || '',
        primaryColor: updated.primary_color || '#3b82f6',
      };
      this.saveSuccess = true;
      return updated;
    } catch (err: unknown) {
      this.saveError =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : 'Failed to update chatbot';
      this.saveSuccess = false;
      return null;
    } finally {
      this.isSaving = false;
    }
  }
}

describe('Chatbot Management Frontend Integration', () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    globalThis.fetch = originalFetch;
  });

  const mockApiChatbot: Chatbot = {
    id: '11111111-1111-1111-1111-111111111111',
    organization_id: 'org-aaa',
    name: 'Support Agent',
    description: 'Handles support requests',
    website_url: 'https://example.com',
    welcome_message: 'Hello, how can I assist you?',
    instructions: 'Be helpful and concise.',
    primary_color: '#3b82f6',
    status: 'draft',
    conversations_count: 0,
    leads_count: 0,
    last_active: '2026-10-20T12:00:00Z',
    created_at: '2026-10-20T12:00:00Z',
    updated_at: '2026-10-20T12:00:00Z',
  };

  describe('API Client Methods (lib/api.ts)', () => {
    it('listChatbots calls GET /api/v1/organizations/{org_id}/chatbots', async () => {
      let capturedUrl = '';
      let capturedMethod = '';

      globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
        capturedUrl = String(input);
        capturedMethod = init?.method || '';
        return new Response(JSON.stringify([mockApiChatbot]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      };

      const result = await listChatbots('org-123', 'test-token');
      assert.equal(capturedUrl, 'http://localhost:8000/api/v1/organizations/org-123/chatbots');
      assert.equal(capturedMethod, 'GET');
      assert.equal(result.length, 1);
      assert.equal(result[0].name, 'Support Agent');
      assert.equal(result[0].conversations_count, 0);
    });

    it('createChatbot calls POST /api/v1/organizations/{org_id}/chatbots with snake_case payload', async () => {
      let capturedUrl = '';
      let capturedMethod = '';
      let capturedBody = '';

      globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
        capturedUrl = String(input);
        capturedMethod = init?.method || '';
        capturedBody = String(init?.body || '');
        return new Response(JSON.stringify(mockApiChatbot), {
          status: 201,
          headers: { 'Content-Type': 'application/json' },
        });
      };

      const payload: ChatbotCreateInput = {
        name: 'New Bot',
        description: 'A new assistant',
        website_url: 'https://example.com',
        primary_color: '#10b981',
      };

      const result = await createChatbot('org-123', payload, 'test-token');
      assert.equal(capturedUrl, 'http://localhost:8000/api/v1/organizations/org-123/chatbots');
      assert.equal(capturedMethod, 'POST');
      assert.deepEqual(JSON.parse(capturedBody), payload);
      assert.equal(result.id, mockApiChatbot.id);
    });

    it('getChatbot calls GET /api/v1/organizations/{org_id}/chatbots/{bot_id}', async () => {
      let capturedUrl = '';

      globalThis.fetch = async (input: RequestInfo | URL) => {
        capturedUrl = String(input);
        return new Response(JSON.stringify(mockApiChatbot), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      };

      const result = await getChatbot('org-123', 'bot-456', 'test-token');
      assert.equal(
        capturedUrl,
        'http://localhost:8000/api/v1/organizations/org-123/chatbots/bot-456',
      );
      assert.equal(result.name, 'Support Agent');
    });

    it('getChatbot throws ApiError on 404 response', async () => {
      globalThis.fetch = async () => {
        return new Response(JSON.stringify({ detail: 'Not found' }), {
          status: 404,
          headers: { 'Content-Type': 'application/json' },
        });
      };

      await assert.rejects(
        async () => {
          await getChatbot('org-123', 'nonexistent');
        },
        (err: unknown) => {
          assert(err instanceof ApiError);
          assert.equal(err.status, 404);
          assert.equal(err.message, 'Not found');
          return true;
        },
      );
    });

    it('updateChatbot calls PATCH /api/v1/organizations/{org_id}/chatbots/{bot_id} with JSON body', async () => {
      let capturedUrl = '';
      let capturedMethod = '';
      let capturedBody = '';

      globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
        capturedUrl = String(input);
        capturedMethod = init?.method || '';
        capturedBody = String(init?.body || '');
        return new Response(
          JSON.stringify({
            ...mockApiChatbot,
            name: 'Updated Agent',
            status: 'active',
          }),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          },
        );
      };

      const payload: ChatbotUpdateInput = {
        name: 'Updated Agent',
        status: 'active',
      };

      const result = await updateChatbot('org-123', 'bot-456', payload, 'test-token');
      assert.equal(
        capturedUrl,
        'http://localhost:8000/api/v1/organizations/org-123/chatbots/bot-456',
      );
      assert.equal(capturedMethod, 'PATCH');
      assert.deepEqual(JSON.parse(capturedBody), payload);
      assert.equal(result.name, 'Updated Agent');
      assert.equal(result.status, 'active');
    });

    it('updateChatbot throws ApiError on failure', async () => {
      globalThis.fetch = async () => {
        return new Response(JSON.stringify({ detail: 'Chatbot not found' }), {
          status: 404,
          headers: { 'Content-Type': 'application/json' },
        });
      };

      await assert.rejects(
        () => updateChatbot('org-123', 'bot-456', { name: 'X' }),
        (err: unknown) => {
          assert.ok(err instanceof ApiError);
          assert.equal(err.status, 404);
          assert.equal(err.message, 'Chatbot not found');
          return true;
        },
      );
    });
  });

  describe('Chatbot List Flow (/chatbots)', () => {
    it('loads and displays chatbots for current organization', async () => {
      globalThis.fetch = async () => {
        return new Response(JSON.stringify([mockApiChatbot]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      };

      const controller = new ChatbotListController();
      assert.equal(controller.isLoading, false);

      await controller.setOrganization('org-aaa');
      assert.equal(controller.isLoading, false);
      assert.equal(controller.error, null);
      assert.equal(controller.chatbots.length, 1);
      assert.equal(controller.chatbots[0].name, 'Support Agent');
      assert.equal(controller.chatbots[0].conversations_count, 0);
    });

    it('handles empty list correctly', async () => {
      globalThis.fetch = async () => {
        return new Response(JSON.stringify([]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      };

      const controller = new ChatbotListController();
      await controller.setOrganization('org-empty');

      assert.equal(controller.isLoading, false);
      assert.equal(controller.error, null);
      assert.equal(controller.chatbots.length, 0);
    });

    it('handles API error and supports retry', async () => {
      let callCount = 0;
      globalThis.fetch = async () => {
        callCount++;
        if (callCount === 1) {
          return new Response(JSON.stringify({ detail: 'Internal Server Error' }), {
            status: 500,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        return new Response(JSON.stringify([mockApiChatbot]), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      };

      const controller = new ChatbotListController();
      await controller.setOrganization('org-err');

      assert.equal(controller.isLoading, false);
      assert.equal(controller.error, 'Internal Server Error');
      assert.equal(controller.chatbots.length, 0);

      // Retry
      await controller.retry();
      assert.equal(controller.isLoading, false);
      assert.equal(controller.error, null);
      assert.equal(controller.chatbots.length, 1);
    });

    it('clears previous chatbots immediately when organization changes and prevents race conditions', async () => {
      let delayedResolve: (res: Response) => void;
      const delayedPromise = new Promise<Response>((resolve) => {
        delayedResolve = resolve;
      });

      globalThis.fetch = async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes('org-slow')) {
          return delayedPromise;
        }
        return new Response(
          JSON.stringify([
            {
              ...mockApiChatbot,
              id: 'bot-fast',
              organization_id: 'org-fast',
              name: 'Fast Bot',
            },
          ]),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          },
        );
      };

      const controller = new ChatbotListController();

      // Start slow request for org-slow
      const slowPromise = controller.setOrganization('org-slow');

      // User immediately switches to org-fast before org-slow resolves
      await controller.setOrganization('org-fast');
      assert.equal(controller.chatbots.length, 1);
      assert.equal(controller.chatbots[0].name, 'Fast Bot');

      // Now resolve the delayed slow request
      delayedResolve!(
        new Response(
          JSON.stringify([
            {
              ...mockApiChatbot,
              id: 'bot-slow',
              organization_id: 'org-slow',
              name: 'Stale Slow Bot',
            },
          ]),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          },
        ),
      );
      await slowPromise;

      // Stale response must NOT overwrite org-fast data!
      assert.equal(controller.chatbots.length, 1);
      assert.equal(controller.chatbots[0].name, 'Fast Bot');
    });
  });

  describe('Chatbot Creation Flow (/chatbots/new)', () => {
    it('validates required name field client-side', async () => {
      const controller = new ChatbotCreateController();
      const res = await controller.submit('org-1', { name: '   ' });

      assert.equal(res, null);
      assert.equal(controller.error, 'Chatbot name is required.');
    });

    it('validates website URL format client-side', async () => {
      const controller = new ChatbotCreateController();
      const res = await controller.submit('org-1', {
        name: 'Valid Name',
        websiteUrl: 'ftp://not-http-or-https',
      });

      assert.equal(res, null);
      assert.equal(controller.error, 'Website URL must start with http:// or https://');
    });

    it('prevents duplicate submissions while request is in-flight', async () => {
      let delayedResolve: (res: Response) => void;
      const delayedPromise = new Promise<Response>((resolve) => {
        delayedResolve = resolve;
      });

      globalThis.fetch = async () => delayedPromise;

      const controller = new ChatbotCreateController();
      const firstSubmission = controller.submit('org-1', { name: 'Bot 1' });
      assert.equal(controller.isSubmitting, true);

      // Attempt duplicate submission
      const secondSubmission = await controller.submit('org-1', { name: 'Bot 2' });
      assert.equal(secondSubmission, null);

      delayedResolve!(
        new Response(JSON.stringify(mockApiChatbot), {
          status: 201,
          headers: { 'Content-Type': 'application/json' },
        }),
      );
      const created = await firstSubmission;
      assert.equal(created?.name, 'Support Agent');
      assert.equal(controller.isSubmitting, false);
    });

    it('handles backend validation or membership error (404/422)', async () => {
      globalThis.fetch = async () => {
        return new Response(JSON.stringify({ detail: 'Organization not found or access denied' }), {
          status: 404,
          headers: { 'Content-Type': 'application/json' },
        });
      };

      const controller = new ChatbotCreateController();
      const res = await controller.submit('org-cross', { name: 'Test Bot' });

      assert.equal(res, null);
      assert.equal(controller.error, 'Organization not found or access denied');
      assert.equal(controller.isSubmitting, false);
    });
  });

  describe('Chatbot Detail Flow (/chatbots/[id])', () => {
    it('retrieves and displays chatbot details', async () => {
      globalThis.fetch = async () => {
        return new Response(JSON.stringify(mockApiChatbot), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      };

      const controller = new ChatbotDetailController();
      await controller.loadDetail('org-aaa', mockApiChatbot.id);

      assert.equal(controller.isLoading, false);
      assert.equal(controller.isNotFound, false);
      assert.equal(controller.error, null);
      assert.notEqual(controller.bot, null);
      assert.equal(controller.bot?.name, 'Support Agent');
      assert.equal(controller.bot?.conversations_count, 0);
      assert.equal(controller.bot?.leads_count, 0);
    });

    it('sets isNotFound=true on 404 (non-existent or cross-tenant access)', async () => {
      globalThis.fetch = async () => {
        return new Response(JSON.stringify({ detail: 'Not found' }), {
          status: 404,
          headers: { 'Content-Type': 'application/json' },
        });
      };

      const controller = new ChatbotDetailController();
      await controller.loadDetail('org-other', mockApiChatbot.id);

      assert.equal(controller.isLoading, false);
      assert.equal(controller.isNotFound, true);
      assert.equal(controller.bot, null);
    });

    it('clears previous chatbot state when organization switches', async () => {
      globalThis.fetch = async () => {
        return new Response(JSON.stringify(mockApiChatbot), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      };

      const controller = new ChatbotDetailController();
      await controller.loadDetail('org-aaa', mockApiChatbot.id);
      assert.notEqual(controller.bot, null);

      // Now switch organization to org-bbb where this bot does not belong (mock 404)
      globalThis.fetch = async () => {
        return new Response(JSON.stringify({ detail: 'Not found' }), {
          status: 404,
          headers: { 'Content-Type': 'application/json' },
        });
      };

      await controller.loadDetail('org-bbb', mockApiChatbot.id);
      assert.equal(controller.bot, null);
      assert.equal(controller.isNotFound, true);
    });

    it('loads and exposes saved instructions and welcome message', async () => {
      globalThis.fetch = async () => {
        return new Response(
          JSON.stringify({
            ...mockApiChatbot,
            welcome_message: 'Custom Hello',
            instructions: 'Custom Prompt Instructions',
          }),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          },
        );
      };

      const controller = new ChatbotDetailController();
      await controller.loadDetail('org-aaa', mockApiChatbot.id);

      assert.equal(controller.bot?.welcome_message, 'Custom Hello');
      assert.equal(controller.bot?.instructions, 'Custom Prompt Instructions');
      assert.equal(controller.formData.welcomeMessage, 'Custom Hello');
      assert.equal(controller.formData.instructions, 'Custom Prompt Instructions');
    });

    it('updates chatbot configuration successfully and updates local state without showing false errors', async () => {
      globalThis.fetch = async () => {
        return new Response(JSON.stringify(mockApiChatbot), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      };

      const controller = new ChatbotDetailController();
      await controller.loadDetail('org-aaa', mockApiChatbot.id);

      globalThis.fetch = async () => {
        return new Response(
          JSON.stringify({
            ...mockApiChatbot,
            name: 'Updated Bot Name',
            description: 'Updated Description',
            welcome_message: 'New Welcome',
            instructions: 'New Instructions',
            primary_color: '#10b981',
          }),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          },
        );
      };

      const updated = await controller.save('org-aaa', mockApiChatbot.id, {
        name: 'Updated Bot Name',
        description: 'Updated Description',
        welcomeMessage: 'New Welcome',
        instructions: 'New Instructions',
        primaryColor: '#10b981',
      });

      assert.notEqual(updated, null);
      assert.equal(controller.bot?.name, 'Updated Bot Name');
      assert.equal(controller.bot?.instructions, 'New Instructions');
      assert.equal(controller.saveSuccess, true);
      assert.equal(controller.saveError, null);
      assert.equal(controller.isSaving, false);
    });

    it('handles status updates to active and inactive', async () => {
      const controller = new ChatbotDetailController();

      globalThis.fetch = async () => {
        return new Response(
          JSON.stringify({
            ...mockApiChatbot,
            status: 'active',
          }),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          },
        );
      };

      const activeResult = await controller.save('org-aaa', mockApiChatbot.id, {
        name: 'Bot',
        status: 'active',
      });
      assert.equal(activeResult?.status, 'active');
      assert.equal(controller.bot?.status, 'active');

      globalThis.fetch = async () => {
        return new Response(
          JSON.stringify({
            ...mockApiChatbot,
            status: 'inactive',
          }),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          },
        );
      };

      const inactiveResult = await controller.save('org-aaa', mockApiChatbot.id, {
        name: 'Bot',
        status: 'inactive',
      });
      assert.equal(inactiveResult?.status, 'inactive');
      assert.equal(controller.bot?.status, 'inactive');
    });

    it('submits existing primary color when saving unrelated settings and preserves custom hex', async () => {
      let capturedPayload: ChatbotUpdateInput | null = null;
      globalThis.fetch = async (_input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.body && typeof init.body === 'string') {
          capturedPayload = JSON.parse(init.body) as ChatbotUpdateInput;
        }
        return new Response(
          JSON.stringify({
            ...mockApiChatbot,
            ...(capturedPayload || {}),
          }),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          },
        );
      };

      const controller = new ChatbotDetailController();
      // Assume existing chatbot was created with shorthand hex '#123'
      controller.formData.primaryColor = '#123';

      // User edits only description, without modifying the primary color
      const updated = await controller.save('org-aaa', mockApiChatbot.id, {
        name: 'Bot Name',
        description: 'New Description Only',
        primaryColor: controller.formData.primaryColor,
      });

      assert.notEqual(updated, null);
      const payload = capturedPayload as ChatbotUpdateInput | null;
      assert.equal(payload?.primary_color, '#123');
      assert.equal(payload?.description, 'New Description Only');
      assert.equal(controller.saveSuccess, true);
    });

    it('validates required name field before saving', async () => {
      const controller = new ChatbotDetailController();
      const res = await controller.save('org-aaa', 'bot-1', { name: '   ' });

      assert.equal(res, null);
      assert.equal(controller.saveError, 'Chatbot name is required.');
      assert.equal(controller.saveSuccess, false);
    });

    it('validates website URL format before saving', async () => {
      const controller = new ChatbotDetailController();
      const res = await controller.save('org-aaa', 'bot-1', {
        name: 'Valid Name',
        websiteUrl: 'ftp://invalid-url.com',
      });

      assert.equal(res, null);
      assert.equal(controller.saveError, 'Website URL must start with http:// or https://');
      assert.equal(controller.saveSuccess, false);
    });

    it('handles API update failure with error message and does not show false success', async () => {
      globalThis.fetch = async () => {
        return new Response(JSON.stringify({ detail: 'Failed to update chatbot' }), {
          status: 500,
          headers: { 'Content-Type': 'application/json' },
        });
      };

      const controller = new ChatbotDetailController();
      const res = await controller.save('org-aaa', 'bot-1', { name: 'New Name' });

      assert.equal(res, null);
      assert.equal(controller.saveError, 'Failed to update chatbot');
      assert.equal(controller.saveSuccess, false);
      assert.equal(controller.isSaving, false);
    });

    it('prevents concurrent duplicate update submissions', async () => {
      let resolveFirstFetch: (res: Response) => void;
      const firstFetchPromise = new Promise<Response>((resolve) => {
        resolveFirstFetch = resolve;
      });

      globalThis.fetch = async () => firstFetchPromise;

      const controller = new ChatbotDetailController();
      const firstSavePromise = controller.save('org-aaa', 'bot-1', { name: 'First Save' });
      const secondSave = await controller.save('org-aaa', 'bot-1', { name: 'Second Save' });

      assert.equal(secondSave, null, 'Second save must be rejected while first is in-flight');

      resolveFirstFetch!(
        new Response(JSON.stringify({ ...mockApiChatbot, name: 'First Save' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      );

      const firstSaveResult = await firstSavePromise;
      assert.equal(firstSaveResult?.name, 'First Save');
    });
  });

  describe('Mock Data Leakage Check', () => {
    it('verifies mockChatbots is not imported or used in the chatbot management routes', () => {
      const listSource = readFileSync(
        resolve(__dirname, '../app/(app)/chatbots/page.tsx'),
        'utf-8',
      );
      const newSource = readFileSync(
        resolve(__dirname, '../app/(app)/chatbots/new/page.tsx'),
        'utf-8',
      );
      const detailSource = readFileSync(
        resolve(__dirname, '../app/(app)/chatbots/[id]/page.tsx'),
        'utf-8',
      );

      assert.equal(
        listSource.includes('mockChatbots'),
        false,
        '/chatbots/page.tsx must not reference mockChatbots',
      );
      assert.equal(
        newSource.includes('mockChatbots'),
        false,
        '/chatbots/new/page.tsx must not reference mockChatbots',
      );
      assert.equal(
        detailSource.includes('mockChatbots'),
        false,
        '/chatbots/[id]/page.tsx must not reference mockChatbots',
      );

      // Verify no hardcoded fake ID generation like bot_new_
      assert.equal(
        newSource.includes('bot_new_'),
        false,
        '/chatbots/new/page.tsx must not use bot_new_ mock IDs',
      );
      assert.equal(
        detailSource.includes('bot_new_'),
        false,
        '/chatbots/[id]/page.tsx must not use bot_new_ mock IDs',
      );
    });

    it('verifies dashboard and recent activity do not import or use mock data', () => {
      const dashboardSource = readFileSync(
        resolve(__dirname, '../app/(app)/dashboard/page.tsx'),
        'utf-8',
      );
      const activitySource = readFileSync(
        resolve(__dirname, '../components/dashboard/recent-activity.tsx'),
        'utf-8',
      );

      assert.equal(
        dashboardSource.includes('@/lib/mock-data'),
        false,
        'app/(app)/dashboard/page.tsx must not import @/lib/mock-data',
      );
      assert.equal(
        dashboardSource.includes('getDashboardStats'),
        false,
        'app/(app)/dashboard/page.tsx must not reference getDashboardStats',
      );
      assert.equal(
        activitySource.includes('@/lib/mock-data'),
        false,
        'components/dashboard/recent-activity.tsx must not import @/lib/mock-data',
      );
      assert.equal(
        activitySource.includes('mockConversations'),
        false,
        'components/dashboard/recent-activity.tsx must not reference mockConversations',
      );
      assert.equal(
        activitySource.includes('mockChatbots'),
        false,
        'components/dashboard/recent-activity.tsx must not reference mockChatbots',
      );

      // Verify dashboard uses real organization-scoped API and displays honest placeholders
      assert.equal(
        dashboardSource.includes('useOrganization'),
        true,
        'app/(app)/dashboard/page.tsx must use useOrganization for tenant scoping',
      );
      assert.equal(
        dashboardSource.includes('listChatbots'),
        true,
        'app/(app)/dashboard/page.tsx must query listChatbots for real chatbot count',
      );
      assert.equal(
        dashboardSource.includes('Analytics not yet available'),
        true,
        'app/(app)/dashboard/page.tsx must display honest placeholder for conversations',
      );
      assert.equal(
        dashboardSource.includes('Lead tracking not yet available'),
        true,
        'app/(app)/dashboard/page.tsx must display honest placeholder for leads',
      );

      // Verify recent activity shows honest empty state
      assert.equal(
        activitySource.includes('No conversations yet'),
        true,
        'components/dashboard/recent-activity.tsx must display honest empty state',
      );
    });
  });
});
