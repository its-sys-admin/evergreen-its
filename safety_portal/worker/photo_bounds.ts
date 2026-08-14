// Photo bounds/shape primitives — EXTRACTED from worker/index.ts (PR-1, 2026-06-12) so the G1
// item-photo route (fieldops_checklist.ts) enforces the EXACT same gate as /api/submit without
// importing index.ts (a runtime import cycle — index.ts registers the fieldops modules) and
// without a clone that could drift (multi-surface fan-out class: a future bounds hardening must
// bite BOTH surfaces or neither). index.ts re-imports these for validatePhotoValues; behavior is
// byte-identical to the pre-extraction originals.
//
// The Worker enforces SHAPE/BOUNDS only — Invariant 2's trust boundary stays Mac-side (§34
// screening) before any Box upload or render. Never log photo bytes.

export const PHOTO_MAX_BYTES = 400_000; // decoded bytes, per photo (client targets ≤ this)
// Screened THUMBNAIL bound (0074): the Mac derives ~320px JPEG q70 thumbs (~15-25KB) from the §34
// clean re-encode; 40KB decoded is comfortably above that and far below anything original-sized,
// so an oversized "thumb" is structurally rejectable at the result/register routes.
export const THUMB_MAX_BYTES = 40_000;
export const B64_RE = /^[A-Za-z0-9+/]+={0,2}$/;

export function b64DecodedLen(s: string): number {
  const pad = s.endsWith("==") ? 2 : s.endsWith("=") ? 1 : 0;
  return Math.floor((s.length * 3) / 4) - pad;
}

/** First decoded bytes must be JPEG (FF D8 FF) or PNG (89 50 4E 47). */
export function photoMagicOk(b64: string): boolean {
  let head: string;
  try {
    head = atob(b64.slice(0, 8));
  } catch {
    return false;
  }
  if (head.length < 4) return false;
  const b = [head.charCodeAt(0), head.charCodeAt(1), head.charCodeAt(2), head.charCodeAt(3)];
  if (b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return true; // JPEG
  return b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4e && b[3] === 0x47; // PNG
}

export const PHOTO_KEYS = ["data", "name", "taken_at", "gps"] as const;

export type PhotoItem = Record<(typeof PHOTO_KEYS)[number], string>;

/** Exact-shape detection ({data,name,taken_at,gps}, all strings) so table-row arrays
 *  (Record<colKey,string>[]) are never misread as photo arrays. */
export function isPhotoItem(x: unknown): x is PhotoItem {
  if (typeof x !== "object" || x === null || Array.isArray(x)) return false;
  const o = x as Record<string, unknown>;
  const keys = Object.keys(o);
  return keys.length === PHOTO_KEYS.length && PHOTO_KEYS.every((k) => typeof o[k] === "string");
}

/**
 * The per-photo bounds gate — the EXACT per-photo checks validatePhotoValues (index.ts) runs,
 * in the EXACT order, returning the same machine reasons. null = OK; string = the machine
 * reason for a 400 { error: "invalid_photo", detail } (same convention as /api/submit).
 */
export function validateSinglePhoto(p: PhotoItem): string | null {
  if (p.name.length > 100 || p.taken_at.length > 40 || p.gps.length > 64) return "photo_meta_too_long";
  if (p.data.length === 0 || p.data.length % 4 !== 0 || !B64_RE.test(p.data)) return "photo_not_base64";
  if (b64DecodedLen(p.data) > PHOTO_MAX_BYTES) return "photo_too_large";
  if (!photoMagicOk(p.data)) return "photo_bad_magic";
  return null;
}
