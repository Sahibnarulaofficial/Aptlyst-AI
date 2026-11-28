/**
 * Framing and HTTP security header builders for Next.js routes.
 *
 * Security model:
 * - Application/dashboard/auth routes (/((?!embed(?:/|$)).*)) MUST NOT be frameable:
 *   enforce both `X-Frame-Options: DENY` and `Content-Security-Policy: frame-ancestors 'none'`.
 * - Public chatbot embed routes (/embed/:path*) require controlled iframe embedding for customer
 *   websites and MUST omit `X-Frame-Options` (which lacks multi-origin support and would block
 *   legitimate cross-origin embeds).
 * - In production, embed framing fails closed to `'self'` unless `EMBED_ALLOWED_FRAME_ANCESTORS`
 *   explicitly configures validated external http(s) origins (normalized via `URL.origin`).
 *   Non-production allows `'self' https: http://localhost:* http://127.0.0.1:*` for local testing
 *   while blocking arbitrary external `http://` origins. Per-chatbot origin binding against
 *   `Chatbot.website_url` requires runtime DB lookup when dynamic interactive embedding ships in
 *   Stage 3C.
 */

export interface NextHeaderEntry {
  key: string;
  value: string;
}

export interface NextHeaderRule {
  source: string;
  headers: NextHeaderEntry[];
}

export interface SecurityHeaderOptions {
  nodeEnv?: string;
  embedAllowedFrameAncestors?: string;
}

const DEFAULT_PROD_FRAME_ANCESTORS = "'self'";
const DEFAULT_DEV_FRAME_ANCESTORS =
  "'self' https: http://localhost:* http://127.0.0.1:*";

function parseAndNormalizeFrameAncestorOrigin(token: string): string | null {
  if (!token || token.includes('*')) {
    return null;
  }
  try {
    const parsed = new URL(token);
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
      return null;
    }
    if (!parsed.hostname || parsed.username || parsed.password) {
      return null;
    }
    if (parsed.pathname !== '/' || parsed.search || parsed.hash) {
      return null;
    }
    return parsed.origin;
  } catch {
    return null;
  }
}

export function resolveEmbedFrameAncestors(
  options: SecurityHeaderOptions = {},
): string {
  const rawList =
    options.embedAllowedFrameAncestors ??
    process.env.EMBED_ALLOWED_FRAME_ANCESTORS ??
    '';
  const trimmed = rawList.trim();

  if (trimmed) {
    const tokens = trimmed
      .split(/[\s,]+/)
      .map((item) => item.trim())
      .filter(Boolean);

    const validatedOrigins: string[] = [];
    for (const token of tokens) {
      const normalizedOrigin = parseAndNormalizeFrameAncestorOrigin(token);
      if (!normalizedOrigin) {
        throw new Error(
          `Invalid EMBED_ALLOWED_FRAME_ANCESTORS entry '${token}': must be an exact http(s) origin without wildcards, paths, queries, or fragments.`,
        );
      }
      if (!validatedOrigins.includes(normalizedOrigin)) {
        validatedOrigins.push(normalizedOrigin);
      }
    }

    if (validatedOrigins.length > 0) {
      return `'self' ${validatedOrigins.join(' ')}`;
    }
  }

  const env = (options.nodeEnv ?? process.env.NODE_ENV ?? 'development')
    .trim()
    .toLowerCase();
  return env === 'production'
    ? DEFAULT_PROD_FRAME_ANCESTORS
    : DEFAULT_DEV_FRAME_ANCESTORS;
}

export function buildNextSecurityHeaders(
  options: SecurityHeaderOptions = {},
): NextHeaderRule[] {
  const embedFrameAncestors = resolveEmbedFrameAncestors(options);
  const commonSecurityHeaders: NextHeaderEntry[] = [
    { key: 'X-Content-Type-Options', value: 'nosniff' },
    { key: 'Referrer-Policy', value: 'strict-origin-when-cross-origin' },
  ];

  return [
    {
      // Apply strict anti-framing to all routes except /embed and /embed/*
      source: '/((?!embed(?:/|$)).*)',
      headers: [
        ...commonSecurityHeaders,
        { key: 'X-Frame-Options', value: 'DENY' },
        { key: 'Content-Security-Policy', value: "frame-ancestors 'none'" },
      ],
    },
    {
      // Controlled framing for public chatbot embed surface
      source: '/embed/:path*',
      headers: [
        ...commonSecurityHeaders,
        {
          key: 'Content-Security-Policy',
          value: `frame-ancestors ${embedFrameAncestors}`,
        },
      ],
    },
  ];
}
