/** The Evergreen job identifier — the ONE place the portal splits and rejoins it.
 *
 *  Evergreen numbers a job site as `YYYY.NNN.S`: `2026.384.1` (MH405) and `2026.384.2`
 *  (OG593) are the same project, two sites. The portal stores those as TWO columns —
 *  `jobs.job_no` holds the two-segment project number, `jobs.site_phase` holds the site
 *  (migration 0064) — because the site segment is exactly what D7 document numbering
 *  already carries: a PO is `{job_no}.{site_phase}.{supersede_seq}.{revision}`, so MH405's
 *  first PO composes as `2026.384.1.0.0`. Folding all three segments into `job_no` would
 *  have produced six-segment document numbers and broken both Mac-side parsers.
 *
 *  The operator never sees that split: they type and read the full identifier, and these two
 *  helpers are the seam. Keep them a matched pair — `splitJobNumber(formatJobNumber(a, b))`
 *  must round-trip for every value the Worker accepts (`tests/jobNumber.test.ts` pins it).
 *
 *  site_phase 0 means NO site segment, matching the D7 default the PO and subcontract
 *  builders already use for a job with no site breakdown — so `2026.384` and a typed
 *  `2026.384.0` are the same job identifier and both display as `2026.384`.
 */

/** The identifier AS TYPED: `YYYY.NNN` or `YYYY.NNN.S`. Mirrors the Worker's
 *  JOB_NO_INPUT_RE (fieldops_job_write.ts) — the two must accept the same strings. */
export const JOB_NUMBER_INPUT_RE = /^(\d{4}\.\d{3})(?:\.(\d+))?$/;

/** Longest identifier the input accepts: `YYYY.NNN.SSSS` (site_phase is capped at 9999
 *  Worker-side, matching po.ts / subcontract.ts). Used as the input's maxLength — the old
 *  value was 8, which silently truncated `2026.384.1` to `2026.384` as the operator typed. */
export const JOB_NUMBER_MAX_LENGTH = 13;

/** Join the stored pair into the identifier the operator recognises.
 *  ('2026.384', 1) → '2026.384.1' · ('2026.384', 0) → '2026.384' · ('', n) → ''. */
export function formatJobNumber(jobNo: string | null | undefined, sitePhase: number | null | undefined): string {
  const base = (jobNo ?? "").trim();
  if (!base) return "";
  const site = sitePhase ?? 0;
  return site > 0 ? `${base}.${site}` : base;
}

/** Split a TYPED identifier into the stored pair, or null when it is not a valid identifier.
 *  An empty/blank string is VALID and means "not yet assigned" — it returns the empty pair,
 *  matching the Worker, which also treats '' as legal rather than malformed. */
export function splitJobNumber(input: string): { job_no: string; site_phase: number } | null {
  const raw = (input ?? "").trim();
  if (!raw) return { job_no: "", site_phase: 0 };
  const m = JOB_NUMBER_INPUT_RE.exec(raw);
  if (!m) return null;
  const site = m[2] !== undefined ? parseInt(m[2], 10) : 0;
  if (site > 9999) return null;
  return { job_no: m[1], site_phase: site };
}
