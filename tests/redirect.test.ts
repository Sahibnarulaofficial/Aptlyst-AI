import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { getSafeRedirectPath } from '../lib/redirect.ts';

describe('getSafeRedirectPath', () => {
  it('allows safe relative paths', () => {
    assert.equal(getSafeRedirectPath('/dashboard'), '/dashboard');
    assert.equal(getSafeRedirectPath('/settings'), '/settings');
    assert.equal(getSafeRedirectPath('/chatbots/new'), '/chatbots/new');
    assert.equal(getSafeRedirectPath('/settings?tab=general&page=2'), '/settings?tab=general&page=2');
    assert.equal(getSafeRedirectPath('/dashboard#metrics'), '/dashboard#metrics');
  });

  it('rejects external absolute URLs', () => {
    assert.equal(getSafeRedirectPath('https://evil.com'), '/dashboard');
    assert.equal(getSafeRedirectPath('http://evil.com/phishing'), '/dashboard');
    assert.equal(getSafeRedirectPath('ftp://evil.com'), '/dashboard');
  });

  it('rejects protocol-relative URLs', () => {
    assert.equal(getSafeRedirectPath('//evil.com'), '/dashboard');
    assert.equal(getSafeRedirectPath('//evil.com/path'), '/dashboard');
    assert.equal(getSafeRedirectPath('///evil.com'), '/dashboard');
  });

  it('rejects backslash bypasses and normalization tricks', () => {
    assert.equal(getSafeRedirectPath('/\\evil.com'), '/dashboard');
    assert.equal(getSafeRedirectPath('/\\\\evil.com'), '/dashboard');
    assert.equal(getSafeRedirectPath('/path\\evil.com'), '/dashboard');
  });

  it('rejects script and pseudo-protocol injection', () => {
    assert.equal(getSafeRedirectPath('javascript:alert(1)'), '/dashboard');
    assert.equal(getSafeRedirectPath('data:text/html,<script>alert(1)</script>'), '/dashboard');
    assert.equal(getSafeRedirectPath('vbscript:msgbox(1)'), '/dashboard');
  });

  it('rejects whitespace-padded attack vectors', () => {
    assert.equal(getSafeRedirectPath('  https://evil.com'), '/dashboard');
    assert.equal(getSafeRedirectPath('  //evil.com'), '/dashboard');
    assert.equal(getSafeRedirectPath('\t//evil.com'), '/dashboard');
    assert.equal(getSafeRedirectPath('\n/valid'), '/valid');
  });

  it('returns fallback for empty, null, or non-string values', () => {
    assert.equal(getSafeRedirectPath(''), '/dashboard');
    assert.equal(getSafeRedirectPath('   '), '/dashboard');
    assert.equal(getSafeRedirectPath(null), '/dashboard');
    assert.equal(getSafeRedirectPath(undefined), '/dashboard');
  });

  it('respects custom fallback path', () => {
    assert.equal(getSafeRedirectPath('https://evil.com', '/custom-fallback'), '/custom-fallback');
    assert.equal(getSafeRedirectPath(null, '/login'), '/login');
    assert.equal(getSafeRedirectPath('//evil.com', '/onboarding'), '/onboarding');
    assert.equal(getSafeRedirectPath('/valid', '/onboarding'), '/valid');
  });
});
