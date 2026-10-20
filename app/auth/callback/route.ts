import { type CookieOptions, createServerClient } from '@supabase/ssr';
import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';
import { getSafeRedirectPath } from '@/lib/redirect';

/**
 * Auth callback handler — called after email verification.
 * Supabase redirects here with ?code=... and exchanges it for a session.
 */
export async function GET(request: NextRequest) {
  const { searchParams, origin } = new URL(request.url);
  const code = searchParams.get('code');
  const safeNext = getSafeRedirectPath(searchParams.get('next'), '/dashboard');

  if (code) {
    const response = NextResponse.redirect(new URL(safeNext, origin));

    const supabase = createServerClient(
      process.env.NEXT_PUBLIC_SUPABASE_URL!,
      process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
      {
        cookies: {
          getAll() {
            return request.cookies.getAll();
          },
          setAll(cookiesToSet: { name: string; value: string; options: CookieOptions }[]) {
            cookiesToSet.forEach(({ name, value, options }) =>
              response.cookies.set(name, value, options),
            );
          },
        },
      },
    );

    const { error } = await supabase.auth.exchangeCodeForSession(code);
    if (!error) {
      return response;
    }
  }

  return NextResponse.redirect(`${origin}/login?error=auth_callback_failed`);
}
