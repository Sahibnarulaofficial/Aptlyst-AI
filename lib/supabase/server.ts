import { type CookieOptions, createServerClient } from '@supabase/ssr';
import { cookies } from 'next/headers';

/**
 * Supabase client for use in Server Components, Server Actions, and Route Handlers.
 * Reads and writes cookies via next/headers — do not call in Client Components.
 */
export async function createClient() {
  const cookieStore = await cookies();

  return createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    (process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY)!,
    {
      cookies: {
        getAll() {
          return cookieStore.getAll();
        },
        setAll(cookiesToSet: { name: string; value: string; options: CookieOptions }[]) {
          try {
            cookiesToSet.forEach(({ name, value, options }) =>
              cookieStore.set(name, value, options),
            );
          } catch {
            // Server Component — cookie writes are handled by the middleware
          }
        },
      },
    },
  );
}
