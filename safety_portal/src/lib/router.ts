/**
 * G2.5 — the portal's URL router (dependency-free, ~100 lines of logic).
 *
 * PATH-BASED routes (e.g. /jobs/JOB-000018), chosen over hash routing on live evidence:
 * wrangler.jsonc ships `not_found_handling: "single-page-application"` with the explicit
 * comment "Client-side routes (e.g. deep links) fall back to index.html with 200", the
 * Worker's catch-all (`app.get("*", ASSETS.fetch)`) sits after the /api/* JSON-404 fence,
 * and a live probe of the deployed Worker returns 200 text/html for a deep path — so
 * path URLs need ZERO Worker changes. The /api/* namespace is fenced by the Worker
 * (JSON 404, never the SPA shell); no route below may ever emit it.
 *
 * Design contract:
 *   • parseRoute(location) → AppRoute | null. null = unrecognized path — the caller
 *     (App) renders home and NORMALIZES the URL, so a mistyped link never blanks.
 *   • formatRoute(route) → canonical "pathname+search" string. Round-trip law (locked
 *     by router.test.ts): parseRoute(formatRoute(r)) deep-equals r for canonical r.
 *   • Capability gating stays in App's view switch, driven by VIEW_CAPS below so the
 *     URL-entry gate and the render gate can never drift. SPA gating is convenience,
 *     never the boundary (Invariant 2) — every action is re-gated server-side.
 *
 * URL-worthy vs ephemeral (grounding decision, G2.5): the Job Tracker's selected job
 * and My Tasks' tab are URL state (the "text the office this job" workflow + the two
 * real tab strips). The tracker's list status FILTER is a <select>, not a tab strip —
 * ephemeral, deliberately not in the URL. FormFillPage's S5 rollup draft `values` are
 * far too large for a URL: the fill route carries the shareable projection (job / form /
 * variant / date) and App keeps the rich payload in memory for the in-session click path.
 */

/** The shareable projection of a FormFillPage prefill (everything but S5 `values`). */
export interface RoutePrefill {
  jobId?: string;
  parentCode?: string;
  variantCode?: string;
  workDate?: string;
}

export type MyTasksTab = "assigned" | "daily";

export type AppRoute =
  | { view: "home" }
  | { view: "login" }
  | { view: "fill"; prefill?: RoutePrefill }
  | { view: "request" }
  | { view: "accounts" }
  | { view: "forms" }
  | { view: "materials-catalog" }
  | { view: "po-builder" }
  | { view: "po-vendors" }
  | { view: "po-config" }
  | { view: "po-estimates" }
  | { view: "po-rfqs" }
  | { view: "subcontractors" }
  | { view: "subcontract-builder" }
  | { view: "fieldops-jobs"; jobId?: string }
  // jobId REQUIRED — this view has no job-less form, so the round-trip law stays total.
  | { view: "fieldops-job-materials"; jobId: string }
  | { view: "fieldops-tasks"; tab?: MyTasksTab }
  | { view: "fieldops-inspections" }
  | { view: "fieldops-equipment" }
  | { view: "fieldops-personnel" };

export const HOME_ROUTE: AppRoute = { view: "home" };

/**
 * Capability required to ENTER each view via URL — mirrors App's render switch exactly
 * (App consumes this same map, so they cannot drift). `null` = any signed-in account;
 * note "fill" and "request" are intentionally ungated here, matching the pre-router
 * switch (the openForm click path must work for checklist-linked accounts regardless
 * of the home-card gate; the server re-validates every submission).
 */
export const VIEW_CAPS: Record<AppRoute["view"], string | null> = {
  home: null,
  login: null,
  fill: null,
  request: null,
  accounts: "cap.admin.accounts",
  forms: "cap.admin.formbuilder",
  "materials-catalog": "cap.materials.manage",
  "po-builder": "cap.po.manage",
  "po-vendors": "cap.po.manage",
  "po-config": "cap.po.manage",
  "po-estimates": "cap.po.manage",
  "po-rfqs": "cap.po.manage",
  subcontractors: "cap.subcontracts.manage",
  "subcontract-builder": "cap.subcontracts.manage",
  "fieldops-jobs": "cap.jobtracker.read",
  // Read is cap-only; the Worker per-job-scopes non-admins, so a submitter reaches only their
  // own job's page, and the write affordances re-gate on materials.manage / the manager role.
  "fieldops-job-materials": "cap.materials.receive",
  "fieldops-tasks": "cap.tasks.own",
  "fieldops-inspections": "cap.checklist.manage",
  "fieldops-equipment": "cap.equipment.field",
  "fieldops-personnel": "cap.personnel.read",
};

/** Simple view ↔ path pairs (everything without params). */
const SIMPLE_PATHS: [Extract<AppRoute, { view: string }>["view"], string][] = [
  ["home", "/"],
  ["login", "/login"],
  ["request", "/request"],
  ["accounts", "/accounts"],
  ["forms", "/forms"],
  ["materials-catalog", "/materials"],
  ["po-builder", "/purchase-orders"],
  ["po-vendors", "/vendors"],
  ["po-config", "/po-config"],
  ["po-estimates", "/purchase-orders/estimates"],
  ["po-rfqs", "/purchase-orders/rfqs"],
  ["subcontract-builder", "/subcontracts"],
  ["subcontractors", "/subcontractors"],
  ["fieldops-inspections", "/checklists"],
  ["fieldops-equipment", "/equipment"],
  ["fieldops-personnel", "/personnel"],
];
const PATH_TO_VIEW = new Map(SIMPLE_PATHS.map(([v, p]) => [p, v]));
const VIEW_TO_PATH = new Map(SIMPLE_PATHS.map(([v, p]) => [v, p]));
// Legacy pre-fold paths: RFQs and Vendor Estimates were top-level pages before they became
// Purchase-Orders tabs. Parse-only aliases (never in SIMPLE_PATHS — formatRoute must keep
// emitting the canonical nested path); App's entry normalization rewrites a cold-loaded
// legacy URL to the canonical one, so pre-fold bookmarks keep landing on the right tab.
PATH_TO_VIEW.set("/estimates", "po-estimates");
PATH_TO_VIEW.set("/rfqs", "po-rfqs");

/** Hygiene cap on URL-borne params (untrusted input; the server is the real boundary). */
const MAX_PARAM = 256;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

function cleanParam(v: string | null): string | undefined {
  if (!v) return undefined;
  const t = v.trim();
  return t.length > 0 && t.length <= MAX_PARAM ? t : undefined;
}

/** Parse a location (pathname + search) into a route; null = unrecognized → caller homes. */
export function parseRoute(loc: { pathname: string; search: string }): AppRoute | null {
  // Canonicalize: strip trailing slashes (except root itself).
  let path = loc.pathname.replace(/\/+$/, "");
  if (path === "") path = "/";

  const simple = PATH_TO_VIEW.get(path);
  if (simple) return { view: simple } as AppRoute;

  if (path === "/tasks") return { view: "fieldops-tasks" };
  if (path === "/tasks/assigned") return { view: "fieldops-tasks", tab: "assigned" };
  if (path === "/tasks/daily") return { view: "fieldops-tasks", tab: "daily" };

  if (path === "/jobs") return { view: "fieldops-jobs" };
  // Materials BEFORE the bare /jobs/:id matcher. That matcher anchors on a slash-free segment so
  // it could not swallow this today, but ordering makes the intent explicit and survives a future
  // loosening of the pattern.
  const matMatch = /^\/jobs\/([^/]+)\/materials$/.exec(path);
  if (matMatch) {
    let raw: string;
    try {
      raw = decodeURIComponent(matMatch[1]);
    } catch {
      return null; // malformed percent-encoding → unrecognized
    }
    const jobId = cleanParam(raw);
    return jobId ? { view: "fieldops-job-materials", jobId } : null;
  }
  const jobMatch = /^\/jobs\/([^/]+)$/.exec(path);
  if (jobMatch) {
    let raw: string;
    try {
      raw = decodeURIComponent(jobMatch[1]);
    } catch {
      return null; // malformed percent-encoding → unrecognized
    }
    const jobId = cleanParam(raw);
    return jobId ? { view: "fieldops-jobs", jobId } : null;
  }

  if (path === "/submit") {
    const q = new URLSearchParams(loc.search);
    const workDate = cleanParam(q.get("date"));
    const prefill: RoutePrefill = {
      jobId: cleanParam(q.get("job")),
      parentCode: cleanParam(q.get("form")),
      variantCode: cleanParam(q.get("variant")),
      workDate: workDate && DATE_RE.test(workDate) ? workDate : undefined,
    };
    const hasAny = Object.values(prefill).some((v) => v !== undefined);
    return hasAny ? { view: "fill", prefill: pruned(prefill) } : { view: "fill" };
  }

  return null;
}

/** Drop undefined keys so route equality (and the round-trip law) is structural. */
function pruned(p: RoutePrefill): RoutePrefill {
  const out: RoutePrefill = {};
  if (p.jobId !== undefined) out.jobId = p.jobId;
  if (p.parentCode !== undefined) out.parentCode = p.parentCode;
  if (p.variantCode !== undefined) out.variantCode = p.variantCode;
  if (p.workDate !== undefined) out.workDate = p.workDate;
  return out;
}

/** Format a route to its canonical URL (pathname + search). */
export function formatRoute(route: AppRoute): string {
  switch (route.view) {
    case "fieldops-tasks":
      return route.tab ? `/tasks/${route.tab}` : "/tasks";
    case "fieldops-jobs":
      return route.jobId ? `/jobs/${encodeURIComponent(route.jobId)}` : "/jobs";
    case "fieldops-job-materials":
      return `/jobs/${encodeURIComponent(route.jobId)}/materials`;
    case "fill": {
      const p = route.prefill;
      if (!p) return "/submit";
      const q = new URLSearchParams();
      if (p.jobId) q.set("job", p.jobId);
      if (p.parentCode) q.set("form", p.parentCode);
      if (p.variantCode) q.set("variant", p.variantCode);
      if (p.workDate) q.set("date", p.workDate);
      const qs = q.toString();
      return qs ? `/submit?${qs}` : "/submit";
    }
    default:
      return VIEW_TO_PATH.get(route.view) ?? "/";
  }
}
