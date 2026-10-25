import { type CookieOptions, createServerClient } from '@supabase/ssr';
import { NextResponse, type NextRequest } from 'next/server';

/**
 * Next.js middleware for Supabase Auth session management.
 *
 * Responsibilities:
 * 1. Refreshes the access token on every request (keeps session alive).
 * 2. Protects dashboard and app routes — redirects unauthenticated users to /login
 *    before the page renders (server-side, not client redirect).
 * 3. Redirects authenticated users away from /login and /signup to /dashboard.
 *
 * Security: authentication is enforced here at the edge, not only client-side.
 */
export async function proxy(request: NextRequest) {
  let supabaseResponse = NextResponse.next({ request });

  const supabase = createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    (process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY)!,
    {
      cookies: {
        getAll() {
          return request.cookies.getAll();
        },
        setAll(cookiesToSet: { name: string; value: string; options: CookieOptions }[]) {
          cookiesToSet.forEach(({ name, value }) =>
            request.cookies.set(name, value),
          );
          supabaseResponse = NextResponse.next({ request });
          cookiesToSet.forEach(({ name, value, options }) =>
            supabaseResponse.cookies.set(name, value, options),
          );
        },
      },
    },
  );

  // Refresh session — do not remove this, it keeps the session alive
  const {
    data: { user },
  } = await supabase.auth.getUser();

  const { pathname } = request.nextUrl;

  const isAuthPage = pathname === '/login' || pathname === '/signup';
  const isProtectedRoute =
    pathname === '/dashboard' ||
    pathname.startsWith('/dashboard/') ||
    pathname === '/chatbots' ||
    pathname.startsWith('/chatbots/') ||
    pathname === '/knowledge' ||
    pathname === '/conversations' ||
    pathname === '/leads' ||
    pathname === '/analytics' ||
    pathname === '/integrations' ||
    pathname === '/billing' ||
    pathname === '/settings';

  // Public runtime routes (/embed/*, /widget.js) are intentionally excluded from
  // isProtectedRoute so website visitors and iframes can access public chatbot previews without logging in.
  if (!user && isProtectedRoute) {
    const url = request.nextUrl.clone();
    url.pathname = '/login';
    url.searchParams.set('next', pathname);
    return NextResponse.redirect(url);
  }

  if (user && isAuthPage) {
    const url = request.nextUrl.clone();
    url.pathname = '/dashboard';
    return NextResponse.redirect(url);
  }

  return supabaseResponse;
}

export const config = {
  matcher: [
    '/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp)$).*)',
  ],
};
