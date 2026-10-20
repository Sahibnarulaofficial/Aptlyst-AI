import { createBrowserClient } from '@supabase/ssr';

/**
 * Supabase client for use in Client Components.
 * Uses the anon key (safe for browser) and relies on cookie-based sessions
 * managed by the @supabase/ssr middleware.
 */
export function createClient() {
  return createBrowserClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    (process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY)!,
  );
}
