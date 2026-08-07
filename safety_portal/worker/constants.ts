// Shared Worker input bounds.
//
// MAX_ADDRESS is the free-text street-address length cap used by every route that accepts
// an address (PO ship-to, subcontract site/contractor, field-ops job create, and the
// ITS_Active_Jobs down-sync in index.ts). It was previously duplicated as a local `const
// MAX_ADDRESS = 512` in po.ts / subcontract.ts / fieldops_job_write.ts AND hardcoded as a
// bare `512` in index.ts's /api/internal/sync bound — four independent copies that agreed
// today but could silently drift if any one were bumped alone (SC-CFG-2). Hoisted here so
// all four import one source of truth.
export const MAX_ADDRESS = 512;

// ── Job lifecycle ───────────────────────────────────────────────────────────────────────────
// `jobs.lifecycle` (migration 0021) is the CANONICAL job-state field. The legacy `status`
// column collapses inactive+archived into 'closed' and `active` is a derived int, so lifecycle
// is the only place the three states are distinguishable.
//
// Hoisted here (rather than left as a local in fieldops_job_write.ts) because the WRITE path
// validates against it and the READ path now coerces with it — one source of truth, and no
// import edge between two sibling route modules.
export const JOB_LIFECYCLES = ["active", "inactive", "archived"] as const;
export type JobLifecycleValue = (typeof JOB_LIFECYCLES)[number];

/** Narrow a stored lifecycle string to the union, defaulting to 'active'.
 *
 *  Fail-SAFE direction is deliberate and is the opposite of an auth coercion: a row predating
 *  0021 (or one written by a future migration this build doesn't know) reads as 'active', i.e.
 *  "still in play". Defaulting to 'archived' would make an unknown value silently hide a live
 *  job from the SPA and — once the archive path exists — mark it as relocatable. The write path
 *  still rejects anything outside the union, so an unknown value can only arrive from the DB,
 *  never from a request. */
export function coerceLifecycle(v: unknown): JobLifecycleValue {
  return typeof v === "string" && (JOB_LIFECYCLES as readonly string[]).includes(v)
    ? (v as JobLifecycleValue)
    : "active";
}
