const fs = require('fs');
const path = require('path');
const { createClient } = require('@supabase/supabase-js');

// 1. Read .env.local for URL and Anon Key (do not log values)
const envPath = path.resolve('.env.local');
const env = {};
if (fs.existsSync(envPath)) {
  const envContent = fs.readFileSync(envPath, 'utf-8');
  envContent.split(/\r?\n/).forEach((line) => {
    const match = line.match(/^([^=]+)=(.*)$/);
    if (match) {
      env[match[1].trim()] = match[2].trim();
    }
  });
}

const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL || env.NEXT_PUBLIC_SUPABASE_URL;
const supabaseKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || env.NEXT_PUBLIC_SUPABASE_ANON_KEY || process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY || env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY;
const apiUrl = process.env.NEXT_PUBLIC_API_URL || env.NEXT_PUBLIC_API_URL || 'http://127.0.0.1:8000';

// 2. Safe Credential Resolution: Prefer temporary local process environment
const testEmail = process.env.TEST_USER_EMAIL || env.TEST_USER_EMAIL || 'sahibnarula106@gmail.com';
const testPassword = process.env.TEST_USER_PASSWORD || env.TEST_USER_PASSWORD || process.env.TEST_PASSWORD || env.TEST_PASSWORD;

if (!supabaseUrl || !supabaseKey) {
  console.error('FAIL: Missing NEXT_PUBLIC_SUPABASE_URL or NEXT_PUBLIC_SUPABASE_ANON_KEY.');
  process.exit(1);
}

if (!testPassword) {
  console.log('STATUS: Awaiting test credentials.');
  console.log('Identified test user email:', testEmail);
  console.log('TEST_USER_PASSWORD is not provided in the environment.');
  process.exit(2);
}

const supabase = createClient(supabaseUrl, supabaseKey);

async function runVerification() {
  console.log('--- Step 1: Signing in via Supabase Auth Client ---');
  console.log('Authenticating test user:', testEmail);

  const { data: authData, error: authError } = await supabase.auth.signInWithPassword({
    email: testEmail,
    password: testPassword,
  });

  if (authError || !authData.session) {
    console.error('FAIL: signInWithPassword failed:', authError ? authError.message : 'No session returned');
    process.exit(1);
  }

  const token = authData.session.access_token;
  console.log('PASS: Supabase session successfully established.');
  console.log('Session user ID:', authData.session.user.id);
  console.log('Session user email:', authData.session.user.email);
  console.log('Token type:', typeof token);

  // Parse header safely without logging the token
  const [headerB64] = token.split('.');
  const headerJson = Buffer.from(headerB64, 'base64url').toString('utf-8');
  const header = JSON.parse(headerJson);
  console.log('PASS: Verified token signing algorithm:', header.alg);
  console.log('PASS: Verified token key identifier (kid):', header.kid);

  if (header.alg !== 'ES256') {
    console.error(`FAIL: Expected ES256 token, got ${header.alg}`);
    process.exit(1);
  }

  console.log('\n--- Step 2: Calling FastAPI Endpoint with Real ES256 Token ---');
  const endpoint = `${apiUrl.replace(/\/$/, '')}/api/v1/organizations`;
  console.log('Target endpoint:', endpoint);

  const response = await fetch(endpoint, {
    method: 'GET',
    headers: {
      'Authorization': `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
  });

  console.log('FastAPI response status:', response.status);
  const responseData = await response.json();
  console.log('FastAPI response body count:', Array.isArray(responseData) ? responseData.length : 'non-array');

  if (response.status !== 200) {
    console.error(`FAIL: Backend rejected token with status ${response.status}`);
    process.exit(1);
  }

  console.log('PASS: Backend accepted real Supabase-issued ES256 token with 200 OK!');

  console.log('\n--- Step 3: Verifying Negative Rejection Cases Against Live Backend ---');
  // Missing token
  const rMissing = await fetch(endpoint);
  console.log('Missing token status:', rMissing.status, '(expected 403)');
  if (rMissing.status !== 403) throw new Error('Missing token did not return 403');

  // Invalid token
  const rInvalid = await fetch(endpoint, {
    headers: { 'Authorization': 'Bearer invalid-token-sample' },
  });
  console.log('Invalid token status:', rInvalid.status, '(expected 401)');
  if (rInvalid.status !== 401) throw new Error('Invalid token did not return 401');

  console.log('\n=== ALL END-TO-END AUTHENTICATION INTEGRATION CHECKS PASSED ===');
}

runVerification().catch((err) => {
  console.error('Unexpected error during verification:', err.message);
  process.exit(1);
});
