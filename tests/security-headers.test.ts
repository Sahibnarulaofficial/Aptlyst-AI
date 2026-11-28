import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
  buildNextSecurityHeaders,
  resolveEmbedFrameAncestors,
} from '../lib/security-headers.ts';

function headerMap(
  entries: { key: string; value: string }[],
): Record<string, string> {
  return Object.fromEntries(entries.map((entry) => [entry.key, entry.value]));
}

describe('Framing and Embedding Security Headers', () => {
  it('blocks all framing on non-embed dashboard and auth routes', () => {
    const rules = buildNextSecurityHeaders({ nodeEnv: 'production' });
    const appRule = rules.find((r) => r.source.includes('?!embed'));
    assert.ok(appRule, 'Expected non-embed security header rule');

    const headers = headerMap(appRule.headers);
    assert.equal(headers['X-Frame-Options'], 'DENY');
    assert.equal(headers['Content-Security-Policy'], "frame-ancestors 'none'");
    assert.equal(headers['X-Content-Type-Options'], 'nosniff');
    assert.equal(headers['Referrer-Policy'], 'strict-origin-when-cross-origin');
  });

  it('fails closed to self-only on /embed/:path* in production when allowlist is missing, empty, or whitespace-only', () => {
    for (const emptyConfig of [undefined, '', '   \t\n  ']) {
      const rules = buildNextSecurityHeaders({
        nodeEnv: 'production',
        embedAllowedFrameAncestors: emptyConfig,
      });
      const embedRule = rules.find((r) => r.source === '/embed/:path*');
      assert.ok(embedRule, 'Expected /embed/:path* security header rule');

      const headers = headerMap(embedRule.headers);
      assert.equal(headers['X-Frame-Options'], undefined);
      assert.equal(headers['Content-Security-Policy'], "frame-ancestors 'self'");
      assert.equal(headers['Content-Security-Policy'].includes('https:'), false);
      assert.equal(headers['Content-Security-Policy'].includes('*'), false);
    }
  });

  it('includes local localhost origins in non-production without permitting arbitrary external HTTP origins', () => {
    const devValue = resolveEmbedFrameAncestors({
      nodeEnv: 'development',
      embedAllowedFrameAncestors: '',
    });
    assert.equal(
      devValue,
      "'self' https: http://localhost:* http://127.0.0.1:*",
    );
    const tokens = devValue.split(/\s+/);
    assert.equal(tokens.includes('http:'), false);
    assert.equal(tokens.includes('*'), false);
  });

  it('normalizes trailing slashes and host casing via URL.origin and deduplicates explicit origins', () => {
    const value = resolveEmbedFrameAncestors({
      nodeEnv: 'production',
      embedAllowedFrameAncestors:
        'https://Docs.Example.com/, https://shop.example.org/ https://docs.example.com',
    });
    assert.equal(value, "'self' https://docs.example.com https://shop.example.org");
  });

  it('rejects wildcard or malformed EMBED_ALLOWED_FRAME_ANCESTORS entries fail-closed', () => {
    for (const bad of [
      '*',
      'https://*.example.com',
      'javascript:alert(1)',
      'https://example.com/path',
      'https://user:pass@example.com',
      'ftp://example.com',
    ]) {
      assert.throws(() =>
        resolveEmbedFrameAncestors({
          nodeEnv: 'production',
          embedAllowedFrameAncestors: bad,
        }),
      );
    }
  });

  it('matches non-embed routes cleanly while excluding /embed and /embed/:id', () => {
    const rules = buildNextSecurityHeaders({ nodeEnv: 'production' });
    const appRule = rules.find((r) => r.source.includes('?!embed'));
    assert.ok(appRule);

    // Strip leading slash and leading capture syntax equivalent to Next.js path-to-regexp
    const nonEmbedPattern = /^\/((?!embed(?:\/|$)).*)$/;
    for (const appPath of [
      '/',
      '/login',
      '/signup',
      '/dashboard',
      '/chatbots',
      '/chatbots/new',
      '/chatbots/123',
      '/billing',
      '/embeddings-info',
    ]) {
      assert.equal(
        nonEmbedPattern.test(appPath),
        true,
        `Expected anti-framing rule to match ${appPath}`,
      );
    }

    for (const embedPath of ['/embed', '/embed/bot-123', '/embed/abc/nested']) {
      assert.equal(
        nonEmbedPattern.test(embedPath),
        false,
        `Expected anti-framing rule NOT to block ${embedPath}`,
      );
    }
  });
});
