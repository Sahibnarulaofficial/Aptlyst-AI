/**
 * Shared frontend validation and normalization helpers for chatbot branding.
 */

export const DEFAULT_CHATBOT_PRIMARY_COLOR = '#3b82f6';
export const HEX_COLOR_PATTERN = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/;

/**
 * Validates and normalizes an optional hex color string.
 *
 * Returns DEFAULT_CHATBOT_PRIMARY_COLOR when empty/nullish, returns the trimmed
 * valid hex string (#rgb or #rrggbb) when valid, or returns null when invalid.
 */
export function normalizeAndValidateHexColor(
  value: string | null | undefined,
): string | null {
  if (value === null || value === undefined) {
    return DEFAULT_CHATBOT_PRIMARY_COLOR;
  }
  const trimmed = value.trim();
  if (!trimmed) {
    return DEFAULT_CHATBOT_PRIMARY_COLOR;
  }
  if (!HEX_COLOR_PATTERN.test(trimmed)) {
    return null;
  }
  return trimmed;
}

/**
 * Converts a 3-digit (#rgb) or 6-digit (#rrggbb) hex color into a 6-digit
 * #rrggbb value safe for native `<input type="color">` elements.
 */
export function toColorPickerValue(value: string | null | undefined): string {
  const normalized = normalizeAndValidateHexColor(value);
  if (!normalized) {
    return DEFAULT_CHATBOT_PRIMARY_COLOR;
  }
  if (normalized.length === 4) {
    const r = normalized[1];
    const g = normalized[2];
    const b = normalized[3];
    return `#${r}${r}${g}${g}${b}${b}`.toLowerCase();
  }
  return normalized.toLowerCase();
}
