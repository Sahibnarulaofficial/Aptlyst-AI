/**
 * Validates and sanitizes a redirect target to guarantee it is an internal path.
 * Protects against open redirect attacks (e.g., protocol-relative URLs,
 * backslash tricks, scheme injection).
 *
 * @param path The candidate redirect path from user input or query parameters.
 * @param fallback The default fallback path if the candidate is unsafe or missing (defaults to '/dashboard').
 * @returns A safe relative path starting with '/'
 */
export function getSafeRedirectPath(
  path: string | null | undefined,
  fallback: string = '/dashboard',
): string {
  if (!path || typeof path !== 'string') {
    return fallback;
  }

  const trimmed = path.trim();

  // Must start with exactly one '/' and NOT '//' or '/\'
  if (!trimmed.startsWith('/') || trimmed.startsWith('//') || trimmed.startsWith('/\\')) {
    return fallback;
  }

  // Reject any backslashes (often used in browser normalization bypasses)
  if (trimmed.includes('\\')) {
    return fallback;
  }

  try {
    // Validate relative URL using standard URL parser with a fixed base
    const parsed = new URL(trimmed, 'https://aptlyst.local');
    if (parsed.origin !== 'https://aptlyst.local') {
      return fallback;
    }
    // Ensure pathname still starts with a single '/'
    if (!parsed.pathname.startsWith('/') || parsed.pathname.startsWith('//')) {
      return fallback;
    }
    // Return relative pathname + search + hash
    return `${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return fallback;
  }
}
