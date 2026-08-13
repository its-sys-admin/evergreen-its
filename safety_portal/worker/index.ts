import { Hono } from "hono";
import { createMiddleware } from "hono/factory";
import type { Context } from "hono";
import { setSignedCookie, getSignedCookie, deleteCookie } from "hono/cookie";
import type { Env, Role, SessionClaims, Vars } from "./types";
import type { FieldopsGates } from "./fieldops_gates";
import { registerPersonnelRoutes } from "./fieldops_personnel";
import { MAX_ADDRESS } from "./constants";
import { registerEquipmentRoutes } from "./fieldops_equipment";
import { registerJobTrackerRoutes } from "./fieldops_jobtracker";
import { registerMaterialsRoutes } from "./fieldops_materials";
import { auditStmt, isUniqueViolation, auditStmtIfChanged } from "./audit";
import { registerTimeWriteRoutes } from "./fieldops_time_write";
import { registerJobWriteRoutes } from "./fieldops_job_write";
import { registerTaskWriteRoutes } from "./fieldops_task_write";
import { registerMyTasksRoutes } from "./fieldops_tasks";
import { registerChecklistRoutes } from "./fieldops_checklist";
import { generateRecurringChecklists } from "./fieldops_recurrence";
import { isDailyTabFamilyForm, registerDailyRequirementsRoutes } from "./fieldops_daily_requirements";
import { requireDailyReportRole } from "./fieldops_scope";
import { registerEquipmentFieldWriteRoutes } from "./fieldops_equipment_write";
import { registerEquipmentRosterWriteRoutes } from "./fieldops_equipment_roster_write";
import { registerPersonnelWriteRoutes } from "./fieldops_personnel_write";
import { registerCrewAssignRoutes } from "./fieldops_crew_assign";
import { registerCrewWriteRoutes } from "./fieldops_crew_write";
import { registerMaterialWriteRoutes } from "./fieldops_material_write";
import { registerExpectedMaterialsRoutes } from "./fieldops_expected_materials";
import {
  claimAdditionalPhotos,
  parseAdditionalPhotoRefs,
  registerDailyPhotoRoutes,
  releaseAllPhotoClaimsStmt,
} from "./fieldops_daily_photos";
import { registerProgressRollupRoutes } from "./fieldops_rollup";
import { registerWeeklyReportRoutes } from "./fieldops_report";
import { registerPoRoutes } from "./po";
import { registerPoAttachmentRoutes } from "./po_attachments";
import { registerManifestRoutes } from "./fieldops_manifests";
import { registerScheduleRoutes } from "./fieldops_schedules";
import { registerScheduleTaskRoutes } from "./fieldops_schedule_tasks";
import { registerPaymentRoutes } from "./fieldops_payments";
import { registerPoEstimateRoutes } from "./po_estimates";
import { registerRfqRoutes } from "./rfq";
import { registerConfigRoutes } from "./config";
import { registerSubcontractRoutes } from "./subcontract";
import {
  validateUser,
  newSessionClaims,
  hashPassword,
  normalizeUsername,
  coerceRole,
  resolveCapabilities,
  parseRole,
} from "./auth";
import { validateCategory, validateDefinition, validateParentGrouping } from "./publishValidation";
import { pruneOldData, writePruneMeta } from "./prune";
import { buildSubmissionInsert } from "./submission";
import { PHOTO_MAX_BYTES, THUMB_MAX_BYTES, b64DecodedLen, photoMagicOk, isPhotoItem, B64_RE } from "./photo_bounds";
import catalog from "../catalog.json";

// ─────────────────────────────────────────────────────────────────────────────
// ITS Safety Portal — Worker API (Phase 2)
//
// Purpose: the single Cloudflare Worker for the Safety Portal. Validates a portal
//   login against D1, issues/verifies an HMAC-signed session cookie, and serves the
//   built React SPA (static assets). Nothing else in Phase 2.
//
// Invariants:
//   - Invariant 1 (External Send Gate): ZERO external transmission — no email, no
//     third-party outbound, no AI step. The only fetch is c.env.ASSETS (asset
//     serving). Phase 5 keeps the Worker SEND-FREE by design: it signs + queues each
//     submission in D1 and serves it over an authenticated /api/internal/pending
//     endpoint; the Mac-side portal_poll daemon PULLS + files (the pull model —
//     decision_phase5-portal-transport). The Worker never sends.
//   - Invariant 2 (Adversarial Input Handling): all browser input is untrusted —
//     request bodies are type-checked + length-bounded; D1 access uses bound
//     parameters (no string interpolation); the session cookie is HttpOnly +
//     HMAC-signed (constant-time verify).
//
// Failure modes: stateless at this layer — Cloudflare owns the process lifecycle,
//   so there is no fail-open/closed posture to maintain here. A D1 error in
//   /api/login propagates and Hono returns 500 (login fails closed). bcrypt.compare
//   at cost 10 can exceed the Workers FREE-plan 10ms CPU cap (Error 1102) — the
//   deployed Worker must be on the Paid plan or swap to PBKDF2 (see README "Deploy").
//   Session validity is cookie-derived only: NO server-side revocation in Phase 2
//   (see the /api/logout rationale).
//
// Consumers: the SPA (src/) via same-origin fetch — /api/login, /api/session,
//   /api/logout, /api/jobs, /api/recent, /api/submit (signs + queues the submission).
//   The Mac-side portal_poll daemon via bearer-token /api/internal/pending (queue
//   drain) + /api/internal/mark-filed (the receipt) + /api/internal/sync (full-replace
//   push of the ITS_Active_Jobs set → the D1 dropdown cache).
// ─────────────────────────────────────────────────────────────────────────────

const COOKIE = "its_portal_session";
const MAX_AGE_S = 60 * 60 * 24 * 90; // 90-day session for submitters (field convenience)
// Admins get a 30-minute IDLE window (slice 8b, C10): a SLIDING cookie re-issued on each
// active request, so an idle (or captured) admin cookie dies at 30 min regardless. The SPA
// pings on activity to keep an actively-used session alive (and logs out proactively at idle);
// while a dirty form-editor draft is open it ADDS a bounded wall-clock keep-alive so unsaved
// work in a briefly-backgrounded tab isn't bounced mid-edit — but an abandoned editor still
// idles out at 30 min (the keep-alive is bounded to the idle window; the draft is client-cached).
const ADMIN_IDLE_S = 30 * 60;

const app = new Hono<{ Bindings: Env; Variables: Vars }>();

// ── Security response headers (audit 2026-06-08: #2 CSP, #3 clickjacking, #8–11) ─
// wrangler.jsonc sets run_worker_first:true so EVERY request runs the Worker — these
// reach the SPA document + static assets too (the platform otherwise serves them and
// bypasses Hono).
//
// CRITICAL (the 2026-06-08 hotfix): responses from c.env.ASSETS.fetch() have IMMUTABLE
// headers. Mutating them in place — which Hono's secureHeaders()/c.header() do — THROWS,
// and under run_worker_first:true that 500'd every static asset AND the SPA document
// (only the Hono-built /api/* responses, which have mutable headers, survived). So we
// RECONSTRUCT each response with a fresh, mutable Headers COPY and set ours on that.
// The copy preserves the asset's own content-type/etag/cache headers; we only ADD.
//
// CSP is ENFORCING (flipped 2026-06-08 after a clean browser smoke: admin login →
// dashboard → a form rendered WITH signature capture produced ZERO CSP violations). It
// shipped Report-Only for one cycle first so the smoke couldn't break the live SPA. The CSP allows
// React inline styles ('unsafe-inline' style-src) + the logo/inline-SVG signature
// (img-src 'self' data:); the built index.html has NO inline <script> → script-src 'self'.
// Cache-Control:no-store is /api/*-ONLY (the cacheable static assets keep their caching).
// script-src/connect-src allow Cloudflare's Web Analytics beacon (auto-injected at the
// edge: static.cloudflareinsights.com serves beacon.min.js, which POSTs RUM data to
// cloudflareinsights.com). Without these the enforcing CSP blocks the beacon → a console
// error every load. Cloudflare's own first-party CDN; everything else stays 'self'.
const CSP =
  "default-src 'self'; " +
  "script-src 'self' https://static.cloudflareinsights.com; " +
  "connect-src 'self' https://cloudflareinsights.com; " +
  "style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; " +
  "base-uri 'self'; frame-ancestors 'none'; form-action 'self'";
app.use("*", async (c, next) => {
  await next();
  const headers = new Headers(c.res.headers); // mutable copy — preserves Set-Cookie, etag, etc.
  headers.set("X-Frame-Options", "DENY");
  headers.set("X-Content-Type-Options", "nosniff");
  headers.set("Referrer-Policy", "strict-origin-when-cross-origin");
  headers.set("Strict-Transport-Security", "max-age=31536000; includeSubDomains");
  headers.set("Content-Security-Policy", CSP);
  if (new URL(c.req.url).pathname.startsWith("/api/")) headers.set("Cache-Control", "no-store");
  c.res = new Response(c.res.body, { status: c.res.status, statusText: c.res.statusText, headers });
});

// Global error handler (audit #1, defense-in-depth). Before this, an unguarded throw
// (e.g. a null-body deref) returned the runtime's bare 500. Return clean JSON with NO
// stack leak; logged to the Worker log (observability), NOT paged — a malformed unauth
// request must never Sentry-spam. This is the backstop BEHIND the per-handler
// body-shape guards added below.
app.onError((err, c) => {
  console.error("worker_unhandled", err instanceof Error ? err.message : String(err));
  return c.json({ error: "internal_error" }, 500);
});

// ── Phase 5 transport (pull model) — HMAC signing + internal-endpoint auth ──────

// canonicalPayload + buildSubmissionInsert — extracted to worker/submission.ts (#17, §14) so the
// checklist-completion emit (worker/fieldops_checklist.ts) mints a BYTE-IDENTICAL submissions row
// with the same 5-field HMAC without importing index.ts (a runtime import cycle). /api/submit builds
// its INSERT via buildSubmissionInsert (imported above); hmacHex now lives entirely behind that
// helper (worker/hmac.ts is still the shared MAC primitive both the submission + item-photo
// protocols call).

/**
 * Length-independent constant-time compare: compares the SHA-256 digests, so the
 * loop runs over fixed 32-byte hashes and leaks NO length oracle on the bearer token
 * (a plain char-by-char compare with an early length-mismatch exit would).
 */
async function safeTokenEqual(a: string, b: string): Promise<boolean> {
  const enc = new TextEncoder();
  const [da, db] = await Promise.all([
    crypto.subtle.digest("SHA-256", enc.encode(a)),
    crypto.subtle.digest("SHA-256", enc.encode(b)),
  ]);
  const ua = new Uint8Array(da);
  const ub = new Uint8Array(db);
  let diff = 0;
  for (let i = 0; i < ua.length; i++) diff |= ua[i] ^ ub[i];
  return diff === 0;
}

/** Bearer-token gate for /api/internal/* — the Mac-side portal_poll daemon's auth. */
const requireInternalToken = createMiddleware<{ Bindings: Env; Variables: Vars }>(async (c, next) => {
  const auth = c.req.header("Authorization") ?? "";
  const token = auth.startsWith("Bearer ") ? auth.slice(7) : "";
  // Fail closed if the token isn't configured (missing secret → reject, never allow).
  if (!token || !c.env.PORTAL_INTERNAL_API_TOKEN || !(await safeTokenEqual(token, c.env.PORTAL_INTERNAL_API_TOKEN))) {
    return c.json({ error: "unauthorized" }, 401);
  }
  await next();
});

/**
 * Bearer-token gate for /api/internal/admin/* — operator user-provisioning.
 * SEPARATE secret from PORTAL_INTERNAL_API_TOKEN (privilege separation): the
 * portal_poll daemon's token must NOT be able to create / reset / disable users.
 * Same fail-closed-on-missing-secret + constant-time posture as requireInternalToken.
 */
const requireAdminToken = createMiddleware<{ Bindings: Env; Variables: Vars }>(async (c, next) => {
  const auth = c.req.header("Authorization") ?? "";
  const token = auth.startsWith("Bearer ") ? auth.slice(7) : "";
  if (!token || !c.env.PORTAL_ADMIN_API_TOKEN || !(await safeTokenEqual(token, c.env.PORTAL_ADMIN_API_TOKEN))) {
    return c.json({ error: "unauthorized" }, 401);
  }
  await next();
});

/**
 * Bearer-token gate for /api/internal/fieldops/* — the Mac-side field-ops mirror daemon
 * (field_ops/fieldops_sync.py, P2.5). SEPARATE secret from PORTAL_INTERNAL_API_TOKEN and
 * PORTAL_ADMIN_API_TOKEN (privilege separation): the mirror daemon's token must NOT be able to
 * drain the submission queue (/api/internal/*) or provision users (/api/internal/admin/*), and
 * neither of those tokens may read/advance the job-mirror queue. Same fail-closed-on-missing-secret
 * + constant-time posture as requireInternalToken.
 */
const requireFieldopsToken = createMiddleware<{ Bindings: Env; Variables: Vars }>(async (c, next) => {
  const auth = c.req.header("Authorization") ?? "";
  const token = auth.startsWith("Bearer ") ? auth.slice(7) : "";
  if (!token || !c.env.PORTAL_FIELDOPS_API_TOKEN || !(await safeTokenEqual(token, c.env.PORTAL_FIELDOPS_API_TOKEN))) {
    return c.json({ error: "unauthorized" }, 401);
  }
  await next();
});

/**
 * Bearer-token gate for /api/po/internal/* — the Mac-side PO daemon (po_materials/po_poll.py,
 * WS1 S4). SEPARATE secret from the portal_poll / admin / fieldops tokens (privilege
 * separation): the PO daemon's token must NOT be able to drain the submission queue,
 * provision users, or touch the job/hours mirror — and none of those tokens may read the PO
 * queue or write PO status/vendor state. Same fail-closed-on-missing-secret + constant-time
 * posture as requireInternalToken. Passed into registerPoRoutes (worker/po.ts).
 */
const requirePoToken = createMiddleware<{ Bindings: Env; Variables: Vars }>(async (c, next) => {
  const auth = c.req.header("Authorization") ?? "";
  const token = auth.startsWith("Bearer ") ? auth.slice(7) : "";
  if (!token || !c.env.PORTAL_PO_API_TOKEN || !(await safeTokenEqual(token, c.env.PORTAL_PO_API_TOKEN))) {
    return c.json({ error: "unauthorized" }, 401);
  }
  await next();
});

/**
 * Bearer-token gate for /api/po/estimates/internal/* — the Mac-side estimate daemon
 * (po_materials/estimate_poll.py, ADR-0004 E2). SEPARATE secret from the PO token and every
 * other tier (privilege separation — ADR-0004 decision 4 / red-team #1): estimate_poll is the
 * highest-exposure process in the system (it decodes hostile PDF/xlsx bytes through
 * pdfplumber/Pillow/Quartz), so its token scopes ONLY the estimate pool — it must NOT be able
 * to read the PO queue, write PO status/vendor state, drain the submission queue, provision
 * users, touch the mirrors, or reach any send-lane control surface (including the future RFQ
 * tier, which mints its OWN token) — and none of those tokens may read the estimate pool.
 * Same fail-closed-on-missing-secret + constant-time posture as requireInternalToken.
 * Passed into registerPoEstimateRoutes (worker/po_estimates.ts).
 */
const requireEstimateToken = createMiddleware<{ Bindings: Env; Variables: Vars }>(async (c, next) => {
  const auth = c.req.header("Authorization") ?? "";
  const token = auth.startsWith("Bearer ") ? auth.slice(7) : "";
  if (!token || !c.env.PORTAL_ESTIMATE_API_TOKEN || !(await safeTokenEqual(token, c.env.PORTAL_ESTIMATE_API_TOKEN))) {
    return c.json({ error: "unauthorized" }, 401);
  }
  await next();
});

/**
 * Bearer-token gate for /api/po/rfqs/internal/* — the Mac-side RFQ daemons
 * (po_materials/rfq_poll.py + rfq_send_poll.py, ADR-0004 R2/R3). SEPARATE secret from BOTH
 * the PO token and the estimate token and every other tier (privilege separation — ADR-0004
 * decision 4 / red-team #1): the estimate daemon is the highest-exposure process in the
 * system (it decodes hostile PDF/xlsx bytes), and a compromise of it must NOT reach the RFQ
 * send-lane control surface — so the RFQ tier mints its OWN token that scopes ONLY
 * /api/po/rfqs/internal/*; it must NOT be able to read the PO queue, the estimate pool,
 * drain the submission queue, provision users, or touch the mirrors — and none of those
 * tokens may read/advance the RFQ queue. Same fail-closed-on-missing-secret + constant-time
 * posture as requireInternalToken. Passed into registerRfqRoutes (worker/rfq.ts).
 */
const requireRfqToken = createMiddleware<{ Bindings: Env; Variables: Vars }>(async (c, next) => {
  const auth = c.req.header("Authorization") ?? "";
  const token = auth.startsWith("Bearer ") ? auth.slice(7) : "";
  if (!token || !c.env.PORTAL_RFQ_API_TOKEN || !(await safeTokenEqual(token, c.env.PORTAL_RFQ_API_TOKEN))) {
    return c.json({ error: "unauthorized" }, 401);
  }
  await next();
});

/**
 * Bearer-token gate for /api/fieldops/manifests/internal/* — the Mac-side manifest daemon
 * (field_ops/manifest_poll.py, PR3b). SEPARATE secret from every other tier, for exactly the
 * reason the estimate lane has its own: this daemon decodes hostile PDF/xlsx bytes (openpyxl
 * + pdfplumber inside a killable child), making it a highest-exposure process, so its token
 * scopes ONLY the manifest pool. It must NOT be able to read the PO / RFQ / estimate queues,
 * drain the submission queue, provision users, touch the mirrors, or reach any send-lane
 * control surface — and none of those tokens may read the manifest pool. Same
 * fail-closed-on-missing-secret + constant-time posture as requireInternalToken. Passed into
 * registerManifestRoutes (worker/fieldops_manifests.ts).
 */
const requireManifestToken = createMiddleware<{ Bindings: Env; Variables: Vars }>(async (c, next) => {
  const auth = c.req.header("Authorization") ?? "";
  const token = auth.startsWith("Bearer ") ? auth.slice(7) : "";
  if (
    !token || !c.env.PORTAL_MANIFEST_API_TOKEN ||
    !(await safeTokenEqual(token, c.env.PORTAL_MANIFEST_API_TOKEN))
  ) {
    return c.json({ error: "unauthorized" }, 401);
  }
  await next();
});

/**
 * Bearer-token gate for /api/fieldops/schedules/internal/* — the Mac-side schedule daemon
 * (field_ops/schedule_poll.py, ADR-0006). SEPARATE secret from every other tier, for exactly the
 * reason the manifest lane has its own: this daemon decodes hostile PDF bytes (Quartz render +
 * Apple Vision OCR inside a killable child), making it a highest-exposure process, so its token
 * scopes ONLY the schedule pool. It must NOT be able to read the manifest / PO / RFQ / estimate
 * queues, drain the submission queue, provision users, touch the mirrors, or reach any send-lane
 * control surface — and none of those tokens may read the schedule pool. Same
 * fail-closed-on-missing-secret + constant-time posture as requireInternalToken. Passed into
 * registerScheduleRoutes (worker/fieldops_schedules.ts).
 */
const requireScheduleToken = createMiddleware<{ Bindings: Env; Variables: Vars }>(async (c, next) => {
  const auth = c.req.header("Authorization") ?? "";
  const token = auth.startsWith("Bearer ") ? auth.slice(7) : "";
  if (
    !token || !c.env.PORTAL_SCHEDULE_API_TOKEN ||
    !(await safeTokenEqual(token, c.env.PORTAL_SCHEDULE_API_TOKEN))
  ) {
    return c.json({ error: "unauthorized" }, 401);
  }
  await next();
});

/**
 * Bearer-token gate for /api/internal/config/* — the Mac-side config daemon (config_editor/
 * config_poll.py, §50 — built LATER). SEPARATE secret from the portal_poll / admin / fieldops /
 * PO tokens (privilege separation): the config daemon's token must NOT be able to drain the
 * submission queue, provision users, touch the job/hours mirror, or read the PO queue — and none
 * of those tokens may read/advance the config-edit queue. Same fail-closed-on-missing-secret +
 * constant-time posture as requireInternalToken. Passed into registerConfigRoutes (worker/config.ts).
 */
const requireConfigToken = createMiddleware<{ Bindings: Env; Variables: Vars }>(async (c, next) => {
  const auth = c.req.header("Authorization") ?? "";
  const token = auth.startsWith("Bearer ") ? auth.slice(7) : "";
  if (!token || !c.env.PORTAL_CONFIG_API_TOKEN || !(await safeTokenEqual(token, c.env.PORTAL_CONFIG_API_TOKEN))) {
    return c.json({ error: "unauthorized" }, 401);
  }
  await next();
});

/**
 * Bearer-token gate for /api/subcontracts/internal/* — the Mac-side subcontract daemon
 * (subcontracts/subcontract_poll.py, SC-S3c/S4). SEPARATE secret from the portal_poll / admin /
 * fieldops / PO / config tokens (privilege separation): the subcontract daemon's token must NOT be
 * able to drain the submission queue, provision users, touch the job/hours mirror, or read the PO
 * or config queues — and none of those tokens may read the subcontract queue or write subcontract
 * status / subcontractor state. Same fail-closed-on-missing-secret + constant-time posture as
 * requireInternalToken. Passed into registerSubcontractRoutes (worker/subcontract.ts).
 */
const requireSubToken = createMiddleware<{ Bindings: Env; Variables: Vars }>(async (c, next) => {
  const auth = c.req.header("Authorization") ?? "";
  const token = auth.startsWith("Bearer ") ? auth.slice(7) : "";
  if (!token || !c.env.PORTAL_SUB_API_TOKEN || !(await safeTokenEqual(token, c.env.PORTAL_SUB_API_TOKEN))) {
    return c.json({ error: "unauthorized" }, 401);
  }
  await next();
});

/**
 * POST /api/login — validate credentials, issue a signed session cookie.
 * `secure` is conditional on HTTPS so login works over http://localhost in
 * `vite dev` while staying Secure on the deployed HTTPS origin.
 */
app.post("/api/login", async (c) => {
  let body: { username?: unknown; password?: unknown };
  try {
    body = await c.req.json();
  } catch {
    return c.json({ error: "bad_request" }, 400);
  }
  // JSON `null`/arrays/scalars PARSE fine but aren't objects; dereferencing body.x on
  // them threw → bare 500 (audit #1). Require a plain object (the `as unknown` cast
  // dodges the no-overlap check on the typed body var).
  if (typeof body !== "object" || (body as unknown) === null || Array.isArray(body)) {
    return c.json({ error: "bad_request" }, 400);
  }

  const username = typeof body.username === "string" ? body.username.trim() : "";
  const password = typeof body.password === "string" ? body.password : "";
  // Bound the inputs; reject obviously-malformed before touching the DB.
  if (!username || !password || username.length > 128 || password.length > 256) {
    return c.json({ error: "invalid_credentials" }, 401);
  }

  const user = await validateUser(c.env, username, password);
  if (!user) return c.json({ error: "invalid_credentials" }, 401);

  const claims = newSessionClaims(user);
  await setSignedCookie(c, COOKIE, JSON.stringify(claims), c.env.SESSION_SIGNING_SECRET, {
    httpOnly: true,
    secure: new URL(c.req.url).protocol === "https:",
    sameSite: "Lax",
    path: "/",
    // Admins start on the short idle window immediately; submitters keep 90 days (8b/C10).
    maxAge: user.role === "admin" ? ADMIN_IDLE_S : MAX_AGE_S,
  });
  // `role` + `capabilities` let the SPA decide which tabs/actions to render. Display-only
  // hinting — every gated action is independently re-gated server-side (requireRole /
  // requireCapability). Caps resolved from D1 (migration 0013); FAIL-CLOSED on error.
  const capabilities = await resolveCapabilities(user.role, c.env.DB);
  return c.json({
    user: { username: user.username, role: user.role, capabilities: [...capabilities] },
    // #16 feature flag (display-hint only — the assign route + cron are the real gates): lets the
    // assign form show the recurring controls when live. NOT a capability (it is site-wide, not
    // per-role), so it rides alongside `user`, not inside its capability set.
    recurring_checklists_enabled: c.env.RECURRING_CHECKLISTS_ENABLED === "true",
    // #17 feature flag (display-hint only — the emit route is the real gate): lets the assigned-
    // inspection view show the "Sign & log to progress report" action only when the feature is live.
    // Same site-wide, NOT-a-capability posture as the #16 flag above.
    checklist_progress_logging_enabled: c.env.CHECKLIST_PROGRESS_LOGGING_ENABLED === "true",
  });
});

/** Verify the signed session cookie; 401 on absent/tampered/expired. */
const requireSession = createMiddleware<{ Bindings: Env; Variables: Vars }>(async (c, next) => {
  // getSignedCookie returns the value if the HMAC verifies (constant-time, via
  // crypto.subtle.verify), false if tampered, undefined if absent — all falsy here.
  const raw = await getSignedCookie(c, c.env.SESSION_SIGNING_SECRET, COOKIE);
  if (!raw) return c.json({ error: "unauthenticated" }, 401);

  let claims: SessionClaims;
  try {
    claims = JSON.parse(raw) as SessionClaims;
  } catch {
    return c.json({ error: "bad_session" }, 401);
  }
  if (typeof claims.iat !== "number") {
    return c.json({ error: "bad_session" }, 401);
  }
  // Reject expired AND future-dated (negative age) sessions — the latter guards a
  // clock-skew / forged-iat edge even though forging iat needs the signing key.
  const ageS = Math.floor(Date.now() / 1000) - claims.iat;
  if (ageS < 0 || ageS > MAX_AGE_S) {
    return c.json({ error: "expired" }, 401);
  }

  // Phase-7 revocation: the session is cookie-derived, but a disabled (or deleted)
  // user must be locked out immediately. Per-request D1 lookup by username
  // (negligible at this scale). FAIL-CLOSED: a missing/disabled user → 401, and any
  // D1 error → 401 too (a DB blip must neither grant access nor crash the request).
  // ORDER DEPENDENCY: migration 0006 (users.disabled) must be live BEFORE this
  // deploys, else this read errors and 401s every session — see README activation.
  // Read `role` in the SAME per-request lookup as `disabled` (migration 0007 adds
  // the column; ORDER DEPENDENCY: it must be live before this deploys). Role is
  // authoritative from D1 here — NOT from the cookie — so a demotion is effective
  // immediately. coerceRole fails safe (unknown → 'submitter', never 'admin').
  // Read `session_epoch` (slice 8a, audit #7) in the SAME lookup — one SELECT returns
  // `disabled + role + session_epoch` (migration 0009 adds the column; ORDER
  // DEPENDENCY: it must be live before this deploys). The epoch is the captured-cookie
  // kill switch: logout / password-change increment the DB column, so an outstanding
  // cookie's snapshot falls BEHIND and is rejected here. A pre-#7 cookie carries NO
  // epoch claim → treated as 0 (== the column DEFAULT), so existing sessions survive.
  let role: Role;
  try {
    const row = await c.env.DB
      .prepare("SELECT disabled, role, session_epoch FROM users WHERE username = ?")
      .bind(claims.username)
      .first<{ disabled: number; role: string; session_epoch: number }>();
    if (!row || row.disabled) {
      return c.json({ error: "revoked" }, 401);
    }
    // Stale-epoch ⇒ revoked. `?? 0` keeps a pre-#7 (no-epoch-claim) cookie valid.
    if ((claims.epoch ?? 0) < row.session_epoch) {
      return c.json({ error: "revoked" }, 401);
    }
    role = coerceRole(row.role);
  } catch {
    return c.json({ error: "unauthenticated" }, 401);
  }

  // Admin 30-min idle timeout (slice 8b, C10) — a SLIDING window. An admin cookie idle
  // past ADMIN_IDLE_S is rejected (a captured admin cookie dies at 30 min); an ACTIVE admin
  // request SLIDES the window by re-issuing the cookie with a fresh iat + 30-min maxAge.
  // Submitters keep the 90-day session. (The MAX_AGE check above already ran; this is the
  // tighter admin window on top, and is authoritative regardless of the cookie's maxAge —
  // a captured cookie whose browser-maxAge was tampered still dies via this iat check.)
  let sessionClaims = claims;
  if (role === "admin") {
    if (ageS > ADMIN_IDLE_S) return c.json({ error: "idle" }, 401);
    sessionClaims = { ...claims, iat: Math.floor(Date.now() / 1000) };
    await setSignedCookie(c, COOKIE, JSON.stringify(sessionClaims), c.env.SESSION_SIGNING_SECRET, {
      httpOnly: true,
      secure: new URL(c.req.url).protocol === "https:",
      sameSite: "Lax",
      path: "/",
      maxAge: ADMIN_IDLE_S,
    });
  }

  c.set("session", sessionClaims);
  c.set("role", role);
  // Resolve the role KEY → capability SET (migration 0013) in the SAME
  // change-effective-next-request posture as `role`. resolveCapabilities is FAIL-CLOSED
  // (unknown role / D1 error → empty set, never privileged). ORDER DEPENDENCY: migration
  // 0013's role_capabilities must be live before this deploys (mirror of 0006/0007/0009).
  c.set("capabilities", await resolveCapabilities(role, c.env.DB));
  await next();
});

/**
 * Session+role gate for the in-app admin surface (/api/admin/*). MUST chain AFTER
 * requireSession, which sets the per-request `role` from D1. A non-admin session
 * → 403 (authenticated but unauthorized). This is the REAL gate for the admin UI —
 * the SPA hiding the admin tabs is never the boundary (Invariant 2: never trust the
 * client). SEPARATE from requireAdminToken: that is a bearer secret for the operator
 * CLI's /api/internal/admin/*; this is a logged-in admin acting in the browser.
 */
const requireRole = (role: Role) =>
  createMiddleware<{ Bindings: Env; Variables: Vars }>(async (c, next) => {
    if (c.get("role") !== role) return c.json({ error: "forbidden" }, 403);
    await next();
  });

/**
 * Fine-grained capability gate (migration 0013). MUST chain AFTER requireSession, which
 * resolves the per-request capability SET from D1. A session lacking the capability → 403.
 * Field-ops READ/field actions gate on capability; admin-surface actions gate on requireRole.
 * FAIL-CLOSED: an empty/missing capability set (unknown role, or a D1 blip in
 * resolveCapabilities → empty Set) → 403. Never trust the client (Invariant 2) — the SPA
 * hiding a card is hinting, this is the boundary. (Was deferred in P0 until its first consumer.)
 */
const requireCapability = (cap: string) =>
  createMiddleware<{ Bindings: Env; Variables: Vars }>(async (c, next) => {
    if (!c.get("capabilities").has(cap)) return c.json({ error: "forbidden" }, 403);
    await next();
  });

/**
 * OR-capability gate — authorizes if the session holds ANY of `caps`. Chains AFTER requireSession
 * (same as requireCapability). FAIL-CLOSED identically: an empty/missing capability set → none
 * match → 403. Used where a route accepts more than one capability (e.g. task create/assign accepts
 * cap.jobtracker.manage OR cap.tasks.assign). A finer-grained per-target guard (e.g. the
 * subcontractor-target check) lives IN the handler, which reads c.get("capabilities") directly.
 */
const requireAnyCapability = (caps: readonly string[]) =>
  createMiddleware<{ Bindings: Env; Variables: Vars }>(async (c, next) => {
    const held = c.get("capabilities");
    if (!caps.some((cap) => held.has(cap))) return c.json({ error: "forbidden" }, 403);
    await next();
  });

// Field-ops READ layer (P2.2). Each tab owns its own route module; the gates are passed IN so
// the per-tab modules never import index.ts (no import cycle). Registered here (before the SPA
// catch-all). In Brief 0 these are no-op stubs; Briefs A/B/C implement them.
const fieldopsGates: FieldopsGates = { requireSession, requireCapability, requireAnyCapability };
registerPersonnelRoutes(app, fieldopsGates);
registerPersonnelWriteRoutes(app, fieldopsGates);
registerEquipmentRoutes(app, fieldopsGates);
registerJobTrackerRoutes(app, fieldopsGates);
registerMaterialsRoutes(app, fieldopsGates);
// — Assigned-Tasks tab (P4 S1) "My Tasks" read (cap.tasks.own) —
registerMyTasksRoutes(app, fieldopsGates);
// — Assigned-Tasks tab (P4 S2) checklist engine + per-job template editor (cap.checklist.manage) —
registerChecklistRoutes(app, fieldopsGates);
// — SOP daily form D4: per-job daily-form requirements (admin CRUD cap.checklist.manage + the
//   ownership-scoped tab read) — the D1 overlay rendered inside the daily form's
//   job_requirements section —
registerDailyRequirementsRoutes(app, fieldopsGates);
// — field-ops WRITE routes (P2.3); send-free D1 mutations, capability-gated, audit-batched —
registerTimeWriteRoutes(app, fieldopsGates);
registerJobWriteRoutes(app, fieldopsGates);
registerTaskWriteRoutes(app, fieldopsGates);
registerEquipmentFieldWriteRoutes(app, fieldopsGates);
registerEquipmentRosterWriteRoutes(app, fieldopsGates);
// — P2.6 crew→job placement (cap.crew.assign; Manager + admin), send-free D1 mutation —
registerCrewAssignRoutes(app, fieldopsGates);
// — Assigned-Tasks Slice T: subcontractor scoped crew-create (cap.crew.create), send-free D1 mutation —
registerCrewWriteRoutes(app, fieldopsGates);
registerMaterialWriteRoutes(app, fieldopsGates);
// — Material receipts M1: per-job expected-materials CRUD + receive/flag (send-free D1) —
registerExpectedMaterialsRoutes(app, fieldopsGates);
// — Materials-manifest import pool (PR3b): the office uploads a BOM / shipping log; the Worker
//   bounds-gates it, signs manifest:v1 and pools the bytes SEND-FREE in D1 (session +
//   cap.materials.manage), and the Mac manifest_poll daemon drains
//   /api/fieldops/manifests/internal/* under its OWN requireManifestToken bearer. Zero parsing
//   here: openpyxl/pdfplumber over untrusted bytes runs on the Mac inside a killable child. —
registerManifestRoutes(app, { requireSession, requireCapability, requireManifestToken });
// — Job-schedule import pool (ADR-0006): the office uploads a project-schedule PDF (Smartsheet
//   Gantt export); the Worker bounds-gates it, signs schedule:v1 and pools the bytes SEND-FREE
//   in D1 (session + cap.jobtracker.manage), and the Mac schedule_poll daemon drains
//   /api/fieldops/schedules/internal/* under its OWN requireScheduleToken bearer. Zero parsing
//   here: Quartz render + Vision OCR over untrusted bytes runs on the Mac inside a killable
//   child. —
registerScheduleRoutes(app, { requireSession, requireCapability, requireScheduleToken });
// — Living schedule task list (ADR-0006 PR-4): the per-job tasks a committed schedule import
//   authors (0071). Read rides cap.jobtracker.read (all roles view — decision 4); the manual
//   add/edit/deactivate floor rides cap.jobtracker.manage. Send-free D1 writes, W4-batched. —
registerScheduleTaskRoutes(app, fieldopsGates);
// — Job payments (ADR-0006 decision 10, PR-7): per-job payment terms + manual invoice cycles +
//   append-only receipts (0073), states DERIVED at read (payments_derive.ts) — never stored.
//   EVERY route session + cap.payments.manage (ADMIN ONLY — operator decision 4: commercially
//   sensitive; payment data appears in NO other route's response). DISPLAY-ONLY: nothing here
//   sends, reminds, or generates a notice (Invariant 1 — alerting is the ADR's later fold-in). —
registerPaymentRoutes(app, fieldopsGates);
// — DR-photo-pool Slice 1: the daily-report additional-photo pool (upload / list / delete;
//   send-free D1 queue for the Slice-2 Mac §34 screen; /api/submit claims the references) —
registerDailyPhotoRoutes(app, fieldopsGates);
// — P6 progress rollup read (bearer-gated /api/internal/*, NOT a session gate) —
registerProgressRollupRoutes(app, requireInternalToken);
// — Weekly Production Report (0067): the client-facing 5-page report's aggregation. TWO gate
//   tiers on ONE derivation — bearer /api/internal/production-report for the Mac compile (same
//   privilege class as the rollup above, no new secret) and session+cap.jobtracker.manage
//   /api/fieldops/weekly-report for the office screen that supplies what D1 cannot derive —
registerWeeklyReportRoutes(app, { requireSession, requireCapability, requireInternalToken });
// — PO workstream S2: vendors cache + drafts/generate/supersede/cancel (session +
//   cap.po.manage) + the /api/po/internal/* queue under the NEW requirePoToken tier —
registerPoRoutes(app, { requireSession, requireCapability, requirePoToken });
// — PO document attachments (Feature B): draft-scoped upload/list/delete (session +
//   cap.po.manage) + the Mac-ward /api/po/internal/attachments/* byte surface under the
//   SAME requirePoToken tier. §34 Option-D: the Worker bounds-gates + pools bytes in D1
//   SEND-FREE; the Mac screens (po_attach_screen) before Box/Smartsheet. —
registerPoAttachmentRoutes(app, { requireSession, requireCapability, requirePoToken });
// — Vendor-estimate importer E1 (ADR-0004): office upload pool + disposition surface (session +
//   cap.po.manage) + the Mac-ward /api/po/estimates/internal/* pool under the NEW
//   requireEstimateToken tier (the extraction daemon's OWN bearer — red-team #1). SEND-FREE, zero
//   AI: the Worker bounds-gates, signs est:v1, and pools bytes in D1; the Mac screens/classifies/
//   extracts, and every dollar re-enters through the human disposition + the EXISTING draft route. —
registerPoEstimateRoutes(app, { requireSession, requireCapability, requireEstimateToken });
// — RFQ composer R1 (ADR-0004): multi-vendor price-free RFQ drafts/generate/cancel (session +
//   cap.po.manage) + the /api/po/rfqs/internal/* queue under the NEW requireRfqToken tier (its
//   OWN bearer, separate from BOTH the PO and estimate tokens — red-team #1). SEND-FREE, zero
//   AI: signs rfq:v1 + queues in D1; the Mac rfq_poll daemon (R2) renders/files per vendor;
//   the separate rfq_send lane (R3) transmits only after F22-verified human approval. —
registerRfqRoutes(app, { requireSession, requireCapability, requireRfqToken });
// — Config-editor queue (§50): generic versioned-config editor (session + per-workstream cap) +
//   the /api/internal/config/* queue under the NEW requireConfigToken tier. SEND-FREE — the Mac
//   config daemon (built LATER) is the sole privileged git-commit/deploy actuator. —
registerConfigRoutes(app, { requireSession, requireCapability, requireConfigToken });
// — Subcontracts workstream SC-S3c: subcontractor cache + drafts/generate/supersede/cancel
//   (session + cap.subcontracts.manage) + the /api/subcontracts/internal/* queue under the NEW
//   requireSubToken tier. SEND-FREE (Invariant 1) — signs the sub:v1 body + queues in D1; the Mac
//   subcontract_poll daemon pulls/renders (.docx/.xlsx)/files; execution approval stays Mac-side (F22). —
registerSubcontractRoutes(app, { requireSession, requireCapability, requireSubToken });

/** GET /api/session — who am I (used by the SPA on load to restore session). Returns
 *  the live role (from requireSession's per-request D1 read), so a demotion drops the
 *  admin tabs on the next session refresh. */
app.get("/api/session", requireSession, (c) => {
  const s = c.get("session");
  return c.json({
    user: { username: s.username, role: c.get("role"), capabilities: [...c.get("capabilities")] },
    // #16 — same display-hint flag as /api/login so an SPA that restores its session (not a fresh
    // login) also learns whether the recurring controls should render.
    recurring_checklists_enabled: c.env.RECURRING_CHECKLISTS_ENABLED === "true",
    // #17 — same display-hint flag as /api/login for a restored session (the "Sign & log" action).
    checklist_progress_logging_enabled: c.env.CHECKLIST_PROGRESS_LOGGING_ENABLED === "true",
  });
});

/**
 * POST /api/logout — clear the session cookie AND server-side revoke it.
 *
 * Slice 8a (audit #7): logout now bumps users.session_epoch, so the just-cleared
 * cookie (which snapshotted the OLD epoch at issue) is now stale and rejected by
 * requireSession on any subsequent request — closing the audit's "logout is
 * client-side only / a captured cookie stays valid to iat+90d" gap. The epoch bump is
 * keyed on the username read from the (verified-signed) cookie; a garbage/absent
 * cookie or a D1 blip still clears the cookie and returns ok (logout must never fail
 * closed — the worst case is a no-op bump, never a stuck-logged-in user).
 */
app.post("/api/logout", async (c) => {
  // Best-effort epoch bump. getSignedCookie returns the value only if the HMAC
  // verifies, so we never bump on a forged username. Any error here is swallowed —
  // the cookie clear below is the contract; the bump is the revocation hardening.
  try {
    const raw = await getSignedCookie(c, c.env.SESSION_SIGNING_SECRET, COOKIE);
    if (raw) {
      const claims = JSON.parse(raw) as SessionClaims;
      if (typeof claims.username === "string") {
        await c.env.DB
          .prepare("UPDATE users SET session_epoch = session_epoch + 1 WHERE username = ?")
          .bind(claims.username)
          .run();
      }
    }
  } catch {
    // swallow — logout still clears the cookie below regardless
  }
  deleteCookie(c, COOKIE, { path: "/" });
  return c.json({ ok: true });
});

/** GET /api/jobs — Active jobs for the dropdown (from D1; the portal never reads Smartsheet).
 *  job_no (0057) rides along so a dropdown pick auto-fills the Evergreen YYYY.NNN number in
 *  every builder ('' when unassigned — the builders fall back to the name-prefix parse).
 *  site_phase (0064) is the identifier's THIRD segment, carried SEPARATELY so the PO and
 *  subcontract builders can auto-fill their own Site/phase input from the job rather than
 *  making the operator re-derive it — see the 0064 header for why it is not folded into
 *  job_no (six-segment document numbers; both Mac-side parsers anchor two segments). */
app.get("/api/jobs", requireSession, async (c) => {
  const { results } = await c.env.DB
    .prepare("SELECT job_id, project_name, job_no, site_phase FROM jobs WHERE active = 1 ORDER BY project_name")
    .all<{ job_id: string; project_name: string; job_no: string; site_phase: number }>();
  return c.json({ jobs: results });
});

/** GET /api/recent?job=&form=&date= — the latest prior submission for Amend prefill.
 *
 *  NO AGE BOUND, deliberately (operator decision 2026-08-13). Prefill reaches back as far as
 *  the row exists — SUBMISSION_RETENTION_DAYS (90) — and that costs nothing: the row is kept
 *  for 90 days regardless, for Box-verification and forensics, and the lookup is an exact seek
 *  on idx_submissions_lookup(job_id, form_code, work_date, created_at) with LIMIT 1. A shorter
 *  window was considered and rejected: it would have removed a working capability to buy
 *  nothing measurable.
 *
 *  Cross-actor prefill is likewise INTENDED, not a leak — anyone who can submit a form may load
 *  and amend a colleague's prior submission on the same job. That is the field workflow.
 *
 *  What changed here is only the gate: `cap.form.submit`, matching POST /api/submit. This route
 *  was the one member of the form family the CS4 Slice-4 enforcement pass missed — its seven
 *  siblings (/api/submit, the three /api/submissions/:uuid/* routes, /api/filed,
 *  /api/filed/months, /api/request-pdfs) all gate on a capability while this gated on bare
 *  requireSession. Per that pass's own lockout analysis the role vocabulary is CLOSED and all
 *  three roles (admin/manager/submitter) hold cap.form.submit, so this LOCKS OUT NOBODY today.
 *  What it buys is the same fail-closed posture (unknown role / D1 blip → empty capability set
 *  → 403) and the ability for a future scoped role to actually withhold prefill.
 *
 *  It is NOT an authorization scope-down: there is no user↔job access model in this portal
 *  (users carries no job linkage and /api/jobs serves every active job to any session), so
 *  every authenticated role can still read any job's payload. That residual is a product
 *  decision — a scoped role or a real job-access model — tracked in docs/tech_debt.md. */
app.get("/api/recent", requireSession, requireCapability("cap.form.submit"), async (c) => {
  const job = c.req.query("job") ?? "";
  const form = c.req.query("form") ?? "";
  const date = c.req.query("date") ?? "";
  if (!job || !form || !date) return c.json({ submission: null });
  const row = await c.env.DB
    .prepare(
      "SELECT submission_uuid, payload_json FROM submissions " +
        "WHERE job_id=? AND form_code=? AND work_date=? ORDER BY created_at DESC LIMIT 1",
    )
    .bind(job, form, date)
    .first<{ submission_uuid: string; payload_json: string }>();
  if (!row) return c.json({ submission: null });
  return c.json({
    submission: { submission_uuid: row.submission_uuid, values: JSON.parse(row.payload_json) },
  });
});

/**
 * POST /api/submit — accept a structured submission, cache it in D1 (Amend
 * prefill), and return success.
 *
 * INVARIANT 1: this Worker still performs ZERO external transmission. The Phase-5
 * email shim (portal-noreply@ → safety@, HMAC-signed) is a SEPARATE component that
 * forwards this payload to intake.py; it is NOT wired here. INVARIANT 2: the body
 * is type-checked + length-bounded; the job_id is verified against D1.
 */
// ── Photo values (PR-1, 2026-06-12) ─────────────────────────────────────────────
// D1-inline transport (owner decision 2026-06-12): site photos ride payload_json as
// base64 JPEG/PNG inside `values`, so the canonicalPayload HMAC covers them with ZERO
// signing changes (regression-locked in test/photos.test.ts). The Worker enforces
// SHAPE/BOUNDS only — Invariant 2's trust boundary stays Mac-side (§34 screening in
// intake, PR-2) before any Box upload or render. worker/types.ts "No R2" stance is
// preserved: D1 remains the transient queue; Box remains the system of record; the
// daily prune already cleans filed rows. Never log photo bytes.
const PHOTO_MAX_PER_FIELD = 4;
const PHOTO_MAX_PER_SUBMISSION = 8;
// PHOTO_MAX_BYTES / B64_RE / b64DecodedLen / photoMagicOk / isPhotoItem — extracted to
// worker/photo_bounds.ts (G1) so the item-photo route enforces the EXACT same per-photo gate;
// imported above, behavior byte-identical.
// Pre-photos cap was 1_000_000 (audit #1 era). D1 row practical ceiling is ~2MB;
// 1_800_000 leaves headroom for the non-photo values + SQL row overhead.
const PAYLOAD_MAX = 1_800_000;
/** null = OK; string = machine reason for a 400 invalid_photo. */
function validatePhotoValues(values: Record<string, unknown>): string | null {
  let total = 0;
  for (const v of Object.values(values)) {
    if (!Array.isArray(v) || v.length === 0 || !v.some(isPhotoItem)) continue;
    if (!v.every(isPhotoItem)) return "mixed_photo_array";
    if (v.length > PHOTO_MAX_PER_FIELD) return "too_many_photos_in_field";
    for (const p of v) {
      if (p.name.length > 100 || p.taken_at.length > 40 || p.gps.length > 64) return "photo_meta_too_long";
      if (p.data.length === 0 || p.data.length % 4 !== 0 || !B64_RE.test(p.data)) return "photo_not_base64";
      if (b64DecodedLen(p.data) > PHOTO_MAX_BYTES) return "photo_too_large";
      if (!photoMagicOk(p.data)) return "photo_bad_magic";
      total += 1;
      if (total > PHOTO_MAX_PER_SUBMISSION) return "too_many_photos";
    }
  }
  return null;
}

// ─────────────────────────────────────────────────────────────────────────────────────────────────
// CS4 Slice 4 Part B — vestigial-cap ENFORCEMENT (cap.form.submit / cap.form.request).
//
// Both capabilities were seeded in migration 0013 and granted to every role, but no route ever
// called requireCapability on them (grep-verified zero enforcement hits) — the grants were
// display-only while the routes gated on bare requireSession. Enforced here at the natural
// surfaces: POST /api/submit → cap.form.submit; the six form-request/download routes
// (/api/submissions/:uuid/{request-pdf,status,pdf}, /api/filed, /api/filed/months,
// /api/request-pdfs — 0013's "Browse + request + download a job's filed forms") →
// cap.form.request.
//
// LOCKOUT ANALYSIS (proved from the migrations before enforcing; the role vocabulary is CLOSED —
// roles are migration-seeded only, no roles-CRUD route exists, parseRole/coerceRole accept only
// the three keys, and users.role is FK-bound to the seeded rows):
//   • cap.form.submit  — submitter (0013 explicit grant) + admin (0013 `SELECT key FROM
//     capabilities` catch-all) + manager (0023 explicit grant). ALL THREE roles hold it.
//   • cap.form.request — the same three grant sources. ALL THREE roles hold it.
// So no existing role loses any current ability: every session that could reach these routes
// yesterday passes the new gate today. What the gate buys: a future scoped role (or a live
// role_capabilities edit) can actually withhold form access, and the FAIL-CLOSED
// resolveCapabilities posture (unknown role / D1 blip → empty set → 403) now covers the portal's
// core submit/request surfaces, not only the field-ops ones.
// (cap.inspection.job — 0013 "File job-level inspections (trenching/QC/etc.)" — is deliberately
// NOT enforced: no dedicated surface exists. Nothing writes the `inspections` table today, and
// job-level inspection FORMS ride this same /api/submit path under cap.form.submit. Enforcement
// waits for the surface to exist rather than inventing one.)
// ─────────────────────────────────────────────────────────────────────────────────────────────────

app.post("/api/submit", requireSession, requireCapability("cap.form.submit"), async (c) => {
  let body: Record<string, unknown>;
  try {
    body = await c.req.json();
  } catch {
    return c.json({ error: "bad_request" }, 400);
  }
  // JSON `null`/arrays/scalars PARSE fine but aren't objects; dereferencing body.x on
  // them threw → bare 500 (audit #1). Require a plain object (the `as unknown` cast
  // dodges the no-overlap check on the typed body var).
  if (typeof body !== "object" || (body as unknown) === null || Array.isArray(body)) {
    return c.json({ error: "bad_request" }, 400);
  }
  const str = (k: string) => (typeof body[k] === "string" ? (body[k] as string) : "");
  const job_id = str("job_id");
  const form_code = str("form_code");
  const work_date = str("work_date");
  const submission_uuid = str("submission_uuid");
  const amends_uuid = typeof body.amends_uuid === "string" ? body.amends_uuid : null;
  const values = body.values;
  if (
    !job_id || !form_code || !work_date || !submission_uuid ||
    job_id.length > 64 || form_code.length > 64 || work_date.length > 10 || submission_uuid.length > 64 ||
    (amends_uuid !== null && amends_uuid.length > 64) ||
    typeof values !== "object" || values === null || Array.isArray(values)
  ) {
    return c.json({ error: "invalid_submission" }, 400);
  }
  // ── Daily-report role gate (operator directive 2026-07-03) ────────────────
  // The SOP daily field report (every launch:"daily-tab" catalog family, matched on the S4
  // parent/-v% convention) is a MANAGER/ADMIN surface: cap.form.submit is held by all three
  // roles, so the cap gate above cannot express "not the subcontractor tier". The SPA hides the
  // Daily tab for submitters, but THIS is the boundary (Invariant 2 — never trust the client):
  // a submitter posting a daily-report form_code directly is 403 forbidden_role, checked before
  // the job lookup so an ineligible role learns nothing about job existence. Gated on the
  // per-request D1 session role (closed vocabulary), not a new capability — see
  // fieldops_scope.requireDailyReportRole.
  if (isDailyTabFamilyForm(form_code)) {
    const roleErr = requireDailyReportRole(c);
    if (roleErr) return roleErr;
  }
  // ── Synthesized-only form gate (#17) ──────────────────────────────────────
  // `checklist-completion*` is MINTED by the checklist-completion emit route (fieldops_checklist.ts
  // POST /api/fieldops/checklist/instance/:id/submit) on a signed, COMPLETE, OWNED inspection — it is
  // NEVER hand-filled through the general Submit-a-Form flow (cap.form.submit is held by all three
  // roles; this route does no ownership / complete / one-shot / dark-gate checks). Reject it here
  // server-side (Invariant 2 — never trust the client; the catalog `launch:"synthesized"` picker-hide
  // is cosmetic, THIS is the boundary) so a client cannot forge a signed "completion" attestation.
  if (form_code.startsWith("checklist-completion")) {
    return c.json({ error: "forbidden_synthesized" }, 403);
  }
  const job = await c.env.DB.prepare("SELECT 1 FROM jobs WHERE job_id=? AND active=1").bind(job_id).first();
  if (!job) return c.json({ error: "unknown_job" }, 422);
  // ── Material-incident line reference gate (M3 Slice 1) ─────────────────────
  // A "Material Incident Report" (material-incident*) MAY reference a specific M2 expected-materials
  // line via an OPTIONAL values.line_uuid — threaded by the daily form's "Report a problem →"
  // deep-link (a submission VALUE, NOT a form field). When present it is the TRUST BOUNDARY
  // (Invariant 2 — never trust the client): the referenced line MUST be an ACTIVE expected-materials
  // line of THIS job, else fail-closed 422 BEFORE any submission INSERT. Absent / empty → a valid
  // MANUAL (unlinked) incident. line_uuid is shape-checked (string, bounded) BEFORE the query; the
  // SQL is PARAMETERIZED (?N + .bind), never interpolated. A malformed reference (non-string /
  // oversized) is fail-closed as an unknown line — it can never name a real one.
  if (form_code.startsWith("material-incident")) {
    const rawLine = (values as Record<string, unknown>).line_uuid;
    if (rawLine !== undefined && rawLine !== null && rawLine !== "") {
      if (typeof rawLine !== "string" || rawLine.length > 64) {
        return c.json({ error: "unknown_material_line" }, 422);
      }
      const line = await c.env.DB
        .prepare("SELECT line_uuid FROM job_expected_materials WHERE job_id = ?1 AND line_uuid = ?2 AND active = 1")
        .bind(job_id, rawLine)
        .first();
      if (!line) return c.json({ error: "unknown_material_line" }, 422);
    }
  }
  // Photo bounds/shape gate (PR-1) — see validatePhotoValues above. Returns the machine
  // reason in `detail` (never the bytes) so the SPA can show a useful message.
  const photoErr = validatePhotoValues(values as Record<string, unknown>);
  if (photoErr) return c.json({ error: "invalid_photo", detail: photoErr }, 400);
  // DR-photo-pool: values.additional_photos, when present, must be a bounded list of
  // {pool_id, caption?} POOL REFERENCES (the bytes never ride the payload — they were uploaded
  // individually to daily_photo_pool). Shape-checked here (400); validated + CLAIMED against the
  // pool below (after the uuid-conflict read, before the INSERT).
  const additionalRefs = parseAdditionalPhotoRefs(values as Record<string, unknown>);
  if (typeof additionalRefs === "string") {
    return c.json({ error: "invalid_additional_photos", detail: additionalRefs }, 400);
  }
  const payload = JSON.stringify(values);
  if (payload.length > PAYLOAD_MAX) return c.json({ error: "too_large" }, 413);

  // ── Submit-as ("filled out as") dual-attribution ──────────────────────────
  // The TRUE actor is the authenticated session user — always recorded, never
  // dropped (safety/audit invariant). `submitted_as` is the OPTIONAL attributed
  // account; absent or === actor means a normal self-submit. A non-self value is a
  // privileged impersonation and the SERVER is the gate (Invariant 2 — the SPA
  // hiding the selector for submitters is never the boundary):
  //   - it REQUIRES the live D1 role be 'admin' (set by requireSession), else 403;
  //   - the target must be a real, ENABLED account, else 422 (never attribute to a
  //     non-existent / locked user).
  const actor = c.get("session").username;
  const requestedAs = typeof body.submitted_as === "string" ? body.submitted_as : "";
  let attributed = actor; // default: self-submit
  const isSubmitAs = requestedAs !== "" && normalizeUsername(requestedAs) !== actor;
  if (isSubmitAs) {
    // Forging submitted_as as a non-admin is REJECTED outright — a submitter must
    // never be able to attribute a submission to someone else.
    if (c.get("role") !== "admin") return c.json({ error: "forbidden" }, 403);
    const target = normalizeUsername(requestedAs);
    if (!target) return c.json({ error: "unknown_attributed_user" }, 422);
    const row = await c.env.DB
      .prepare("SELECT disabled FROM users WHERE username=?")
      .bind(target)
      .first<{ disabled: number }>();
    if (!row || row.disabled) return c.json({ error: "unknown_attributed_user" }, 422);
    attributed = target;
  }

  // Fail closed on a misconfigured Worker: never sign with an undefined secret
  // (that would produce signatures the Mac side could never verify → silent loss).
  // buildSubmissionInsert (below) does the signing; the guard stays HERE so the 503
  // is returned before any DB write, exactly as before.
  if (!c.env.HMAC_PAYLOAD_SECRET) return c.json({ error: "server_misconfigured" }, 503);
  // The submission is signed + inserted by buildSubmissionInsert (worker/submission.ts) so the
  // checklist-completion emit mints a byte-identical row. The SPA mints a FRESH uuid per amendment
  // (useSubmissionId), so a same-uuid re-submit is the designed lost-ACK RETRY, not an amendment;
  // the M1 guard below rejects a cross-actor uuid reuse and audits a filed/changed same-actor
  // replace. INSERT OR REPLACE resets box_verified=0 so a retry re-queues for filing. CRITICAL: the
  // canonicalPayload (HMAC input) is UNCHANGED by submit-as — actor_username/submitted_as are NOT
  // part of it — so the stored hmac is byte-identical to a normal submit and portal_poll's recompute
  // still verifies. (Regression-locked in test/submit-as.test.ts.)
  //
  // The submission INSERT carries the two attribution columns (always written; on a
  // self-submit both equal `actor`). On a REAL submit-as we also write an audit_log
  // row in the SAME D1 batch, so the impersonation record can never land without its
  // security-log entry (atomic — mirrors the /api/admin/* mutate+audit pattern).
  // M1 (PR-4): an INSERT OR REPLACE silently overwrites any prior row for this uuid. Read it
  // first — a DIFFERENT actor reusing the uuid is never legitimate (409); a SAME-actor re-submit is
  // the designed retry (proceed) but is AUDITED when the prior row was already filed
  // (box_verified=1) or the payload changed (the filed PDF would then diverge from the new D1 row).
  const existing = await c.env.DB
    .prepare("SELECT actor_username, payload_json, box_verified FROM submissions WHERE submission_uuid=?")
    .bind(submission_uuid)
    .first<{ actor_username: string; payload_json: string; box_verified: number }>();
  if (existing && existing.actor_username !== actor) {
    return c.json({ error: "uuid_conflict" }, 409);
  }
  const isReplace =
    existing !== null && (existing.box_verified === 1 || existing.payload_json !== payload);

  // ── DR-photo-pool: CLAIM the referenced pool photos (claim-first, before the INSERT). ─────
  // Every ref must exist, belong to (job_id, work_date), be the TRUE actor's own upload, not be
  // refused, and be unclaimed — or claimed by THIS uuid (the lost-ACK same-uuid retry) or by the
  // VERIFIED amends target (an amendment transfers the filed report's claims). The claim is atomic
  // (guard-in-WHERE in one D1 batch; a lost double-claim race is compensated inside and returns
  // 409 with zero footprint). Claim-first means a submission row NEVER exists with unclaimed
  // refs; if the INSERT below fails, the rows stay claimed by this uuid and the SPA's same-uuid
  // retry passes the claim guard — self-healing (orphaned claims are Slice-2 prune territory).
  if (additionalRefs !== null && additionalRefs.length > 0) {
    // amends_uuid is RAW BODY DATA (Invariant 2) — verify it before it can appear in the
    // claim-transfer predicate: the target must EXIST in submissions and belong to THIS
    // (job_id, work_date) family. Anything unverifiable (unknown uuid, foreign job/date)
    // degrades to NO-TRANSFER (null) — the claim then sees a foreign claim marker and 409s —
    // so a hostile body naming a random / foreign submission's uuid can never move claims
    // through the transfer arm. (The submissions.amends_uuid COLUMN below still stores the raw
    // declared link — amendment lineage for intake/display, never a claim predicate.)
    let verifiedAmendsUuid: string | null = null;
    if (amends_uuid !== null) {
      const amendsTarget = await c.env.DB
        .prepare("SELECT job_id, work_date FROM submissions WHERE submission_uuid=?")
        .bind(amends_uuid)
        .first<{ job_id: string; work_date: string }>();
      if (amendsTarget && amendsTarget.job_id === job_id && amendsTarget.work_date === work_date) {
        verifiedAmendsUuid = amends_uuid;
      }
    }
    const claim = await claimAdditionalPhotos(c, additionalRefs, {
      submissionUuid: submission_uuid,
      jobId: job_id,
      workDate: work_date,
      actor,
      amendsUuid: verifiedAmendsUuid,
    });
    if (!claim.ok) return c.json({ error: claim.error }, claim.status);
  }

  // buildSubmissionInsert re-derives payload_json = JSON.stringify(values) (byte-identical to the
  // `payload` computed above and used for the PAYLOAD_MAX / M1-replace checks) and signs the
  // canonical 5-field HMAC — one definition shared with the checklist-completion emit.
  const insertStmt = await buildSubmissionInsert(c.env.DB, c.env.HMAC_PAYLOAD_SECRET, {
    submission_uuid,
    job_id,
    form_code,
    work_date,
    values,
    actor,
    submitted_as: attributed,
    amends_uuid,
  });
  const stmts = [insertStmt];
  // DR-photo-pool: a same-uuid REPLACE/retry whose payload carries NO refs (key absent OR an
  // empty list — parseAdditionalPhotoRefs distinguishes both from a malformed list) never runs
  // the claim path above, so its in-batch stale-claim release never fires — without this
  // release-all, a re-submit that dropped every ref would STRAND its prior claims (rows invisible
  // to the pre-submit pool list, undeletable, counted against the day's cap forever). Batched
  // WITH the INSERT so the ref-free payload and its claim release land atomically. Gated on the
  // M1 read: a fresh uuid holds no claims, and the common no-photo submit stays a single run().
  if ((additionalRefs === null || additionalRefs.length === 0) && existing !== null) {
    stmts.push(releaseAllPhotoClaimsStmt(c, submission_uuid));
  }
  if (isSubmitAs) stmts.push(auditStmt(c, actor, "submit_as", attributed, { submission_uuid, job_id }));
  if (isReplace) {
    stmts.push(auditStmt(c, actor, "submission_replace", attributed, {
      submission_uuid, job_id,
      was_filed: existing!.box_verified === 1,
      payload_changed: existing!.payload_json !== payload,
    }));
  }
  if (stmts.length > 1) {
    await c.env.DB.batch(stmts);
  } else {
    await insertStmt.run();
  }
  return c.json({ ok: true, status: "submitted", submission_uuid });
});

// ─────────────────────────────────────────────────────────────────────────────
// Request-driven canonical PDF download (PR-4 Part A).
//
// Owner decision: the PM's downloadable copy IS the Box-filed copy, byte-identical
// (NO browser render). It is request-driven — nothing is cached until the user clicks
// "Make available for download". The Worker is SEND-FREE and holds NO Box creds: the
// Mac-side portal_poll daemon fetches the filed PDF from Box (by box_file_id),
// base64-chunks it, and POSTs the chunks to D1 (POST /api/internal/filed-pdf); GET
// /pdf reassembles the D1 chunks and serves the bytes. Cached chunks expire 24h past
// pdf_ready_at (prune.ts) and are re-requestable.
//
// ACCESS (Part A): the session username must equal submissions.actor_username (the
// TRUE actor who hit submit) OR submissions.submitted_as (the attributed account), OR
// the session role is 'admin'. EVERYONE ELSE → 404 (no enumeration), NOT 403. A row
// that does not exist is likewise 404 — the two are indistinguishable to the caller.
// ─────────────────────────────────────────────────────────────────────────────

/** The PDF-cache ownership row shape (the columns the 3 session routes select). */
interface PdfOwnRow {
  actor_username: string | null;
  submitted_as: string | null;
}
/**
 * Ownership gate for the session+ownership PDF routes. An admin sees any row; a
 * non-admin must be the true actor OR the attributed account. A missing row (null)
 * fails — the caller returns 404 (no 403, no enumeration).
 */
function ownsRow(row: PdfOwnRow | null, c: Context<{ Bindings: Env; Variables: Vars }>): boolean {
  if (!row) return false;
  if (c.get("role") === "admin") return true;
  const me = c.get("session").username;
  return row.actor_username === me || row.submitted_as === me;
}

/** Decode a base64 string to bytes (no length validation here — callers bound it). */
function b64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/**
 * POST /api/submissions/:uuid/request-pdf — mark a filed submission for caching.
 * Flips pdf_requested 0→1 (idempotent: a second request is a no-op). Audits ONLY the
 * real flip. Returns whether the cache is already ready. A rejected (box_verified=-1)
 * row is treated as not-found (404) — there is no PDF to serve.
 */
app.post("/api/submissions/:uuid/request-pdf", requireSession, requireCapability("cap.form.request"), async (c) => {
  const uuid = c.req.param("uuid");
  if (!uuid || uuid.length > 64) return c.json({ error: "not_found" }, 404);
  const row = await c.env.DB
    .prepare(
      "SELECT actor_username, submitted_as, job_id, box_verified, pdf_ready_at FROM submissions WHERE submission_uuid=?",
    )
    .bind(uuid)
    .first<{ actor_username: string | null; submitted_as: string | null; job_id: string; box_verified: number; pdf_ready_at: number | null }>();
  // Not found, not owned, or a rejected (bad-HMAC terminal) row → 404, no enumeration.
  if (!ownsRow(row, c) || row!.box_verified === -1) return c.json({ error: "not_found" }, 404);

  // changes() > 0 ⟺ a real 0→1 flip (the WHERE pdf_requested=0 makes a repeat a no-op);
  // audit ONLY on the flip via the changes()=1 conditional insert (same atomic-batch
  // pattern as mark-rejected). The flag-set + its audit run in ONE D1 batch.
  const res = await c.env.DB.batch([
    c.env.DB.prepare(
      "UPDATE submissions SET pdf_requested=1 WHERE submission_uuid=? AND pdf_requested=0",
    ).bind(uuid),
    auditStmtIfChanged(c, c.get("session").username, "request_pdf", null, { job_id: row!.job_id }),
    // PR-5: downloads are REQUESTER-BOUND — the submitter's request is the first
    // pdf_requests row (one row per submission+account). Re-request refreshes the 24h
    // window. submissions.pdf_requested stays as the legacy flag; pdf_requests is now the
    // authority for who may download and the Mac-serviceable set.
    c.env.DB.prepare(
      "INSERT INTO pdf_requests (submission_uuid, account, requested_at) VALUES (?,?,unixepoch()) " +
        "ON CONFLICT(submission_uuid, account) DO UPDATE SET requested_at=unixepoch(), ready_at=NULL",
    ).bind(uuid, c.get("session").username),
  ]);
  void res;
  // ready = the cache is already populated (pdf_ready_at set AND a chunk row exists).
  const chunk = row!.pdf_ready_at !== null
    ? await c.env.DB.prepare("SELECT 1 FROM filed_pdfs WHERE submission_uuid=? LIMIT 1").bind(uuid).first()
    : null;
  return c.json({ ok: true, ready: row!.pdf_ready_at !== null && chunk !== null });
});

/**
 * GET /api/submissions/:uuid/status — the SPA's 5s poll. Reports whether the user has
 * requested caching, whether the cache is ready to download, and when it expires.
 */
app.get("/api/submissions/:uuid/status", requireSession, requireCapability("cap.form.request"), async (c) => {
  const uuid = c.req.param("uuid");
  if (!uuid || uuid.length > 64) return c.json({ error: "not_found" }, 404);
  const row = await c.env.DB
    .prepare("SELECT actor_username, submitted_as, box_verified, pdf_ready_at FROM submissions WHERE submission_uuid=?")
    .bind(uuid)
    .first<{ actor_username: string | null; submitted_as: string | null; box_verified: number; pdf_ready_at: number | null }>();
  if (!row || row.box_verified === -1) return c.json({ error: "not_found" }, 404);

  // PR-5: REQUESTER-CENTRIC body. `requested` + the 24h `expires_at` come from THIS account's
  // own live pdf_requests row; `ready` additionally needs the cache populated.
  const me = c.get("session").username;
  const pr = await c.env.DB
    .prepare("SELECT requested_at FROM pdf_requests WHERE submission_uuid=? AND account=? AND requested_at > unixepoch()-86400")
    .bind(uuid, me)
    .first<{ requested_at: number }>();
  // Gate (no row-data enumeration): only an admin, the owner/attributee, or a live requester
  // may poll; everyone else gets the same 404 as an unknown uuid, leaking no row contents. (A
  // benign timing residual remains — an existing-but-unauthorized uuid does the second read —
  // matching /pdf; accepted, since UUIDs are unguessable.)
  const ownerOrAdmin = c.get("role") === "admin" || row.actor_username === me || row.submitted_as === me;
  if (!ownerOrAdmin && pr === null) return c.json({ error: "not_found" }, 404);
  const chunk = row.pdf_ready_at !== null
    ? await c.env.DB.prepare("SELECT 1 FROM filed_pdfs WHERE submission_uuid=? LIMIT 1").bind(uuid).first()
    : null;
  const cacheReady = row.pdf_ready_at !== null && chunk !== null;
  const requested = pr !== null;
  const ready = cacheReady && (requested || c.get("role") === "admin");
  const expires_at = pr ? pr.requested_at + 86_400 : null;
  return c.json({ requested, ready, expires_at });
});

/**
 * GET /api/submissions/:uuid/pdf — reassemble the cached chunks and serve the canonical
 * PDF as an attachment. 404 if not owned / not found / not yet cached. The Response is
 * BUILT DIRECTLY (a Hono-built Response with mutable headers) — never mutate an
 * ASSETS.fetch() response (the immutable-headers gotcha); the outer middleware re-wraps
 * it, preserving Content-Type/Content-Disposition and adding Cache-Control:no-store.
 */
app.get("/api/submissions/:uuid/pdf", requireSession, requireCapability("cap.form.request"), async (c) => {
  const uuid = c.req.param("uuid");
  if (!uuid || uuid.length > 64) return c.json({ error: "not_found" }, 404);
  const row = await c.env.DB
    .prepare(
      "SELECT s.box_verified, s.form_code, s.work_date, s.pdf_ready_at, j.project_name " +
        "FROM submissions s LEFT JOIN jobs j ON j.job_id = s.job_id WHERE s.submission_uuid=?",
    )
    .bind(uuid)
    .first<{ box_verified: number; form_code: string; work_date: string; pdf_ready_at: number | null; project_name: string | null }>();
  if (!row || row.box_verified === -1) return c.json({ error: "not_found" }, 404);
  // PR-5: REQUESTER-BOUND. Admins always; otherwise the session account must hold a LIVE
  // pdf_requests row (requested within 24h) for this uuid. A DIFFERENT authenticated account —
  // even the actor/attributee who never requested — gets 404 (the staged PDF is private to
  // its requester; no enumeration).
  if (c.get("role") !== "admin") {
    const pr = await c.env.DB
      .prepare("SELECT 1 FROM pdf_requests WHERE submission_uuid=? AND account=? AND requested_at > unixepoch()-86400")
      .bind(uuid, c.get("session").username)
      .first();
    if (!pr) return c.json({ error: "not_found" }, 404);
  }
  if (row!.pdf_ready_at === null) return c.json({ error: "not_ready" }, 404);

  const { results } = await c.env.DB
    .prepare("SELECT chunk_b64 FROM filed_pdfs WHERE submission_uuid=? ORDER BY chunk_index")
    .bind(uuid)
    .all<{ chunk_b64: string }>();
  if (!results || results.length === 0) return c.json({ error: "not_ready" }, 404);

  // Decode each chunk to bytes, then concat into a single Uint8Array (the original PDF).
  const parts = results.map((r) => b64ToBytes(r.chunk_b64));
  const total = parts.reduce((n, p) => n + p.length, 0);
  const bytes = new Uint8Array(total);
  let off = 0;
  for (const p of parts) {
    bytes.set(p, off);
    off += p.length;
  }
  // Job-prefixed <job>_<work_date>_<form>.pdf to match the Box-filed naming scheme
  // (2026-06-17). Spaces are allowed (the header value is quoted); the rest is sanitized to
  // a safe set so the filename can never break the Content-Disposition header. Falls back to
  // the unprefixed name when the job row is gone (LEFT JOIN → project_name null).
  const jobName = (row!.project_name ?? "").trim();
  const safe = `${jobName ? jobName + "_" : ""}${row!.work_date}_${row!.form_code}.pdf`
    .replace(/[^A-Za-z0-9._ -]/g, "");
  return new Response(bytes, {
    headers: {
      "Content-Type": "application/pdf",
      "Content-Disposition": `attachment; filename="${safe}"`,
    },
  });
});

/**
 * GET /api/filed?job_id=… — PR-5 browse: an ACTIVE job's filed forms with THIS account's
 * per-row request/ready state. requireSession. 404 unless the job is active (browse is
 * scoped to active jobs). Metadata only — no payloads, no PDFs.
 */
app.get("/api/filed", requireSession, requireCapability("cap.form.request"), async (c) => {
  const job_id = c.req.query("job_id") ?? "";
  if (!job_id || job_id.length > 64) return c.json({ error: "not_found" }, 404);
  // PR-6 optional cascade filters. Empty-string ("?month=") is treated as ABSENT (no filter,
  // no 400). month → the work-month (substr of work_date); form_code → the exact form code.
  const month = c.req.query("month") || undefined;
  if (month !== undefined && !/^\d{4}-\d{2}$/.test(month)) {
    return c.json({ error: "bad_request", detail: "month" }, 400);
  }
  const form_code = c.req.query("form_code") || undefined;
  if (form_code !== undefined && form_code.length > 64) {
    return c.json({ error: "bad_request", detail: "form_code" }, 400);
  }
  const active = await c.env.DB
    .prepare("SELECT 1 FROM jobs WHERE job_id=? AND active=1")
    .bind(job_id)
    .first();
  if (!active) return c.json({ error: "not_found" }, 404);
  // LEFT JOIN this account's LIVE request (the 24h window is in the ON clause, so an expired
  // request stops matching). ready = the cache is populated AND this account has a live request.
  // month/form_code add BOUND WHERE terms only; the per-account join, ordering, defensive
  // LIMIT 500, and response shape are unchanged from PR-5. Bind order follows placeholder order:
  // [account (the JOIN's pr.account=?), job_id, month?, form_code?].
  const where =
    "WHERE s.job_id=? AND s.box_verified=1" +
    (month !== undefined ? " AND substr(s.work_date,1,7) = ?" : "") +
    (form_code !== undefined ? " AND s.form_code = ?" : "");
  const binds: unknown[] = [c.get("session").username, job_id];
  if (month !== undefined) binds.push(month);
  if (form_code !== undefined) binds.push(form_code);
  const { results } = await c.env.DB
    .prepare(
      "SELECT s.submission_uuid, s.form_code, s.work_date, s.filed_at, " +
        "(s.pdf_ready_at IS NOT NULL) AS cache_ready, (pr.requested_at IS NOT NULL) AS requested " +
        "FROM submissions s " +
        "LEFT JOIN pdf_requests pr ON pr.submission_uuid=s.submission_uuid AND pr.account=? " +
        "AND pr.requested_at > unixepoch()-86400 " +
        where + " " +
        "ORDER BY s.filed_at DESC, s.created_at DESC LIMIT 500",
    )
    .bind(...binds)
    .all<{ submission_uuid: string; form_code: string; work_date: string; filed_at: number | null; cache_ready: number; requested: number }>();
  const filed = (results ?? []).map((r) => ({
    submission_uuid: r.submission_uuid,
    form_code: r.form_code,
    work_date: r.work_date,
    filed_at: r.filed_at,
    requested: r.requested === 1,
    ready: r.cache_ready === 1 && r.requested === 1,
  }));
  return c.json({ filed });
});

/**
 * GET /api/filed/months?job_id=… — PR-6 cascade source for the Form Request page. Returns
 * the work-months that actually have filed forms (newest-first, each with a count) and the
 * distinct form codes present for the job, so a year-long job's hundreds of filed forms don't
 * dump in one flat 500-capped table. requireSession. 404 unless the job is active (same guard
 * + {error:"not_found"} shape as /api/filed — no enumeration). Job-scoped aggregates only; no
 * per-account state leaks (unlike /api/filed's per-row request/ready flags).
 */
app.get("/api/filed/months", requireSession, requireCapability("cap.form.request"), async (c) => {
  const job_id = c.req.query("job_id") ?? "";
  if (!job_id || job_id.length > 64) return c.json({ error: "not_found" }, 404);
  const active = await c.env.DB
    .prepare("SELECT 1 FROM jobs WHERE job_id=? AND active=1")
    .bind(job_id)
    .first();
  if (!active) return c.json({ error: "not_found" }, 404);
  const monthsRes = await c.env.DB
    .prepare(
      "SELECT substr(work_date,1,7) AS month, COUNT(*) AS count " +
        "FROM submissions WHERE job_id=? AND box_verified=1 " +
        "GROUP BY month ORDER BY month DESC",
    )
    .bind(job_id)
    .all<{ month: string; count: number }>();
  const codesRes = await c.env.DB
    .prepare("SELECT DISTINCT form_code FROM submissions WHERE job_id=? AND box_verified=1 ORDER BY form_code")
    .bind(job_id)
    .all<{ form_code: string }>();
  return c.json({
    months: (monthsRes.results ?? []).map((r) => ({ month: r.month, count: r.count })),
    form_codes: (codesRes.results ?? []).map((r) => r.form_code),
  });
});

/**
 * POST /api/request-pdfs — PR-5 batch request. requireSession. Body { uuids: string[] }
 * (cap 20). For each uuid that is a FILED submission on an ACTIVE job, upsert a pdf_requests
 * row for the session account (refreshing the 24h window). Any authenticated account may
 * request any active-job filed form (mirrors the submit model); the download is then bound
 * to THIS requester. ONE audit row per batch. Returns { requested: <count upserted> }.
 */
app.post("/api/request-pdfs", requireSession, requireCapability("cap.form.request"), async (c) => {
  let body: Record<string, unknown>;
  try {
    body = await c.req.json();
  } catch {
    return c.json({ error: "bad_request" }, 400);
  }
  if (typeof body !== "object" || (body as unknown) === null || Array.isArray(body)) {
    return c.json({ error: "bad_request" }, 400);
  }
  const raw = body.uuids;
  if (!Array.isArray(raw) || raw.length === 0) return c.json({ error: "bad_request", detail: "uuids" }, 400);
  if (raw.length > 20) return c.json({ error: "too_many", detail: "max 20 per batch" }, 400);
  const clean = [...new Set(raw.filter((u): u is string => typeof u === "string" && u.length > 0 && u.length <= 64))];
  if (clean.length === 0) return c.json({ requested: 0 });
  // Only FILED submissions on ACTIVE jobs are requestable.
  const placeholders = clean.map(() => "?").join(",");
  const { results } = await c.env.DB
    .prepare(
      "SELECT s.submission_uuid FROM submissions s JOIN jobs j ON j.job_id=s.job_id " +
        `WHERE s.submission_uuid IN (${placeholders}) AND s.box_verified=1 AND j.active=1`,
    )
    .bind(...clean)
    .all<{ submission_uuid: string }>();
  const valid = (results ?? []).map((r) => r.submission_uuid);
  if (valid.length === 0) return c.json({ requested: 0 });
  const account = c.get("session").username;
  const stmts = valid.map((u) =>
    c.env.DB.prepare(
      "INSERT INTO pdf_requests (submission_uuid, account, requested_at) VALUES (?,?,unixepoch()) " +
        "ON CONFLICT(submission_uuid, account) DO UPDATE SET requested_at=unixepoch(), ready_at=NULL",
    ).bind(u, account),
  );
  stmts.push(auditStmt(c, account, "request_pdfs", null, { count: valid.length, uuids: valid }));
  await c.env.DB.batch(stmts);
  return c.json({ requested: valid.length });
});

/**
 * GET /api/internal/pending — the queue drain for the Mac-side portal_poll daemon.
 * Returns unfiled submissions (box_verified=0) oldest-first, each with the Worker's
 * HMAC so the daemon verifies integrity before intake files it. Bearer-token gated.
 *
 * DR-photo-pool Slice 2: each row also carries `daily_photos` — the CLAIM MANIFEST
 * of daily_photo_pool rows this submission claimed at submit ({id, status,
 * box_file_id}, resolved in ONE batched query over the partial claim index). intake
 * resolves the HMAC-covered values.additional_photos references against it (clean →
 * Box download by box_file_id; pending → bounded defer; refused → PDF note). The
 * manifest is deliberately NOT HMAC-covered — status/box_file_id are server state
 * that changes AFTER signing (the refs themselves ARE covered inside payload_json;
 * intake consumes manifest entries only for referenced pool_ids, and re-validates
 * the downloaded bytes before render).
 */
app.get("/api/internal/pending", requireInternalToken, async (c) => {
  const limit = Math.min(Number(c.req.query("limit")) || 50, 200);
  const { results } = await c.env.DB
    .prepare(
      "SELECT submission_uuid, job_id, form_code, work_date, payload_json, amends_uuid, hmac, created_at " +
        "FROM submissions WHERE box_verified = 0 ORDER BY created_at ASC LIMIT ?",
    )
    .bind(limit)
    .all();
  // Attach each submission's daily-photo claim manifest (empty array when it claimed
  // none — the overwhelmingly common case; one indexed query for the whole page).
  const rows = (results ?? []) as Record<string, unknown>[];
  if (rows.length > 0) {
    const uuids = rows.map((r) => String(r.submission_uuid));
    const placeholders = uuids.map((_, i) => `?${i + 1}`).join(",");
    const claims = await c.env.DB
      .prepare(
        `SELECT id, status, box_file_id, claimed_by_submission FROM daily_photo_pool ` +
          `WHERE claimed_by_submission IN (${placeholders})`,
      )
      .bind(...uuids)
      .all<{ id: number; status: string; box_file_id: string | null; claimed_by_submission: string }>();
    const byUuid = new Map<string, { id: number; status: string; box_file_id: string | null }[]>();
    for (const cl of claims.results ?? []) {
      const list = byUuid.get(cl.claimed_by_submission) ?? [];
      list.push({ id: cl.id, status: cl.status, box_file_id: cl.box_file_id });
      byUuid.set(cl.claimed_by_submission, list);
    }
    for (const r of rows) r.daily_photos = byUuid.get(String(r.submission_uuid)) ?? [];
  }
  return c.json({ pending: rows });
});

/**
 * POST /api/internal/mark-filed — the receipt. intake calls this after it files a
 * submission to Smartsheet + Box; flips box_verified=1 so the queue drains and the
 * portal can show "received & filed." Idempotent. Bearer-token gated.
 */
app.post("/api/internal/mark-filed", requireInternalToken, async (c) => {
  let body: Record<string, unknown>;
  try {
    body = await c.req.json();
  } catch {
    return c.json({ error: "bad_request" }, 400);
  }
  // JSON `null`/arrays/scalars PARSE fine but aren't objects; dereferencing body.x on
  // them threw → bare 500 (audit #1). Require a plain object (the `as unknown` cast
  // dodges the no-overlap check on the typed body var).
  if (typeof body !== "object" || (body as unknown) === null || Array.isArray(body)) {
    return c.json({ error: "bad_request" }, 400);
  }
  const submission_uuid = typeof body.submission_uuid === "string" ? body.submission_uuid : "";
  const box_link = typeof body.box_link === "string" ? body.box_link.slice(0, 2000) : null;
  // box_file_id (PR-4): the filed Box file id the pdf-cache pass downloads + chunks. The
  // daemon supplies it on the receipt; bounded like box_link. NULL when not supplied.
  const boxFileId = typeof body.box_file_id === "string" ? body.box_file_id.slice(0, 200) : null;
  if (!submission_uuid || submission_uuid.length > 64) return c.json({ error: "invalid" }, 400);
  const res = await c.env.DB
    .prepare("UPDATE submissions SET box_verified=1, filed_at=unixepoch(), box_link=?, box_file_id=? WHERE submission_uuid=?")
    .bind(box_link, boxFileId, submission_uuid)
    .run();
  return c.json({ ok: true, found: (res.meta?.changes ?? 0) > 0 });
});

/**
 * POST /api/internal/mark-rejected — terminal state (M4, PR-4) for a submission the Mac side
 * refuses to file (a bad-HMAC row). Without this, a box_verified=0 row is re-served by /pending
 * EVERY cycle forever. Sets box_verified=-1 (terminal — /pending selects =0, so it drops out) on
 * an UNFILED row only; records the reason in audit_log (changes()=1 so a no-op write logs nothing).
 * prune.ts deletes rejected rows after 30d. Idempotent. Bearer-token gated.
 */
app.post("/api/internal/mark-rejected", requireInternalToken, async (c) => {
  let body: Record<string, unknown>;
  try {
    body = await c.req.json();
  } catch {
    return c.json({ error: "bad_request" }, 400);
  }
  if (typeof body !== "object" || (body as unknown) === null || Array.isArray(body)) {
    return c.json({ error: "bad_request" }, 400);
  }
  const submission_uuid = typeof body.submission_uuid === "string" ? body.submission_uuid : "";
  const reason = typeof body.reason === "string" ? body.reason.slice(0, 2000) : null;
  if (!submission_uuid || submission_uuid.length > 64) return c.json({ error: "invalid" }, 400);
  const res = await c.env.DB.batch([
    c.env.DB.prepare(
      "UPDATE submissions SET box_verified=-1, filed_at=unixepoch() WHERE submission_uuid=? AND box_verified=0",
    ).bind(submission_uuid),
    auditStmtIfChanged(c, "portal_poll", "submission_rejected", null, { submission_uuid, reason }),
  ]);
  return c.json({ ok: true, found: (res[0]?.meta?.changes ?? 0) > 0 });
});

// ── PR-4 Part A: the canonical-PDF cache servicing endpoints (Mac portal_poll pass) ──
// Both bearer-gated (requireInternalToken — the daemon token, NOT the admin one). The
// daemon GETs the serviceable set, downloads each from Box, base64-chunks it, and POSTs
// the chunks here. Idempotent: a re-served request after a lost receipt is a no-op.
const MAX_CHUNKS = 8;
const CHUNK_B64_RE = /^[A-Za-z0-9+/]+={0,2}$/;
const CHUNK_DECODED_MAX = 1_000_000;
// Cap the b64 STRING length before the O(n) regex scan so an oversized chunk_b64 is
// rejected in O(1) without traversing the whole string (defence-in-depth DoS guard).
const MAX_CHUNK_B64_LEN = Math.ceil((CHUNK_DECODED_MAX * 4) / 3) + 4; // ~1,333,338

/**
 * GET /api/internal/pdf-requests — the serviceable set for the Mac pdf-cache pass:
 * filed rows with a LIVE pdf_requests row (someone requested within 24h), not yet cached
 * (pdf_ready_at IS NULL), and filed (box_file_id IS NOT NULL — a Box file to download).
 * Oldest-first.
 * Returns a NAMED field (never a bare array — portal_client._request rejects non-object
 * JSON). Bearer-token gated.
 */
app.get("/api/internal/pdf-requests", requireInternalToken, async (c) => {
  const limit = Math.min(Math.max(parseInt(c.req.query("limit") || "25", 10) || 25, 1), 100);
  const { results } = await c.env.DB
    .prepare(
      "SELECT s.submission_uuid, s.box_file_id, s.form_code, s.work_date FROM submissions s " +
        "WHERE s.pdf_ready_at IS NULL AND s.box_file_id IS NOT NULL AND s.box_verified=1 " +
        "AND EXISTS (SELECT 1 FROM pdf_requests pr WHERE pr.submission_uuid=s.submission_uuid " +
        "AND pr.requested_at > unixepoch()-86400) " +
        "ORDER BY s.filed_at LIMIT ?",
    )
    .bind(limit)
    .all();
  return c.json({ pdf_requests: results });
});

/**
 * POST /api/internal/filed-pdf — idempotent chunk upload. The daemon POSTs each
 * base64 chunk (index + total + bytes); when the row count reaches chunk_total the
 * cache is complete and pdf_ready_at is stamped. INSERT OR REPLACE makes a re-POST of
 * the same chunk a no-op. If pdf_ready_at is already set the upload is a no-op
 * (idempotent — already cached). Bearer-token gated.
 */
app.post("/api/internal/filed-pdf", requireInternalToken, async (c) => {
  let body: Record<string, unknown>;
  try {
    body = await c.req.json();
  } catch {
    return c.json({ error: "bad_request" }, 400);
  }
  if (typeof body !== "object" || (body as unknown) === null || Array.isArray(body)) {
    return c.json({ error: "bad_request" }, 400);
  }
  const submission_uuid = typeof body.submission_uuid === "string" ? body.submission_uuid : "";
  const chunk_index = body.chunk_index;
  const chunk_total = body.chunk_total;
  const chunk_b64 = body.chunk_b64;
  // Type + bounds validation (Invariant 2: all daemon input is untrusted too).
  if (!submission_uuid || submission_uuid.length > 64) {
    return c.json({ error: "invalid_chunk", detail: "submission_uuid" }, 400);
  }
  if (typeof chunk_index !== "number" || !Number.isInteger(chunk_index) || chunk_index < 0) {
    return c.json({ error: "invalid_chunk", detail: "chunk_index" }, 400);
  }
  if (typeof chunk_total !== "number" || !Number.isInteger(chunk_total) || chunk_total < 1 || chunk_total > MAX_CHUNKS) {
    return c.json({ error: "invalid_chunk", detail: "chunk_total" }, 400);
  }
  if (chunk_index >= chunk_total) {
    return c.json({ error: "invalid_chunk", detail: "chunk_index_range" }, 400);
  }
  if (
    typeof chunk_b64 !== "string" ||
    chunk_b64.length === 0 ||
    chunk_b64.length > MAX_CHUNK_B64_LEN ||
    !CHUNK_B64_RE.test(chunk_b64)
  ) {
    return c.json({ error: "invalid_chunk", detail: "chunk_b64" }, 400);
  }
  if (b64DecodedLen(chunk_b64) > CHUNK_DECODED_MAX) {
    return c.json({ error: "invalid_chunk", detail: "chunk_too_large" }, 400);
  }

  // The row must exist AND be filed (box_verified=1) — never cache an unfiled / rejected row.
  const row = await c.env.DB
    .prepare("SELECT box_verified, pdf_ready_at FROM submissions WHERE submission_uuid=?")
    .bind(submission_uuid)
    .first<{ box_verified: number; pdf_ready_at: number | null }>();
  if (!row || row.box_verified !== 1) return c.json({ error: "not_found" }, 404);
  // Already cached → idempotent no-op (a re-served request after a lost receipt).
  if (row.pdf_ready_at !== null) return c.json({ ok: true, ready: true, stored: false });

  await c.env.DB
    .prepare(
      "INSERT OR REPLACE INTO filed_pdfs (submission_uuid, chunk_index, chunk_total, chunk_b64) VALUES (?,?,?,?)",
    )
    .bind(submission_uuid, chunk_index, chunk_total, chunk_b64)
    .run();
  // Completion is gated on a CONSISTENT, GAP-FREE set — not a bare COUNT===chunk_total,
  // which a buggy/forged daemon could satisfy with the wrong indices (e.g. {0,1,5} for
  // chunk_total=3) and make GET /pdf serve a silently-truncated PDF as the canonical
  // record. Require: all chunks agree on chunk_total (totals===1), there are exactly t
  // chunks (n===t), and the highest index is t-1 (maxidx===t-1). Distinct indices (the
  // PRIMARY KEY) with count t and max t-1 ⇒ exactly {0..t-1}, i.e. gap-free.
  const agg = await c.env.DB
    .prepare(
      "SELECT COUNT(*) AS n, COUNT(DISTINCT chunk_total) AS totals, MAX(chunk_total) AS t, MAX(chunk_index) AS maxidx FROM filed_pdfs WHERE submission_uuid=?",
    )
    .bind(submission_uuid)
    .first<{ n: number; totals: number; t: number; maxidx: number }>();
  const n = agg?.n ?? 0;
  const complete = agg?.totals === 1 && n === agg.t && agg.maxidx === agg.t - 1;
  if (complete) {
    // Stamp ready once, only on the first completion (the WHERE pdf_ready_at IS NULL
    // guard keeps a racing duplicate completion idempotent).
    await c.env.DB
      .prepare("UPDATE submissions SET pdf_ready_at=unixepoch() WHERE submission_uuid=? AND pdf_ready_at IS NULL")
      .bind(submission_uuid)
      .run();
  }
  return c.json({ ok: true, ready: complete, stored: true, received: n });
});

// ── G1 Slice 2: the checklist item-photo screening queue (Mac portal_poll pass) ──
// Both bearer-gated with requireInternalToken — the SAME middleware instance as
// GET /api/internal/pending (byte-identical gate: fail-closed on a missing secret,
// constant-time compare). The Mac's _service_item_photos pass GETs the pending queue,
// verifies each row's HMAC (shared/portal_hmac.verify_item_photo), runs the
// byte-identical §34 photo_screen pipeline, files a CLEAN sanitized re-encode to Box,
// and POSTs the disposition back here. Option D (RATIFIED 2026-07-03): no route ever
// serves these bytes to a browser; DELETE-ON-SCREEN — the result application NULLs
// photo_json, so D1 holds photo bytes only while status='pending'.
const ITEM_PHOTO_RESULT_STATUSES = new Set(["clean", "refused"]);

/**
 * GET /api/internal/item-photos/pending — the unscreened queue, oldest-first.
 * Each row carries the VERBATIM stored photo_json + the Worker's HMAC so the Mac
 * verifies integrity before any byte is decoded (the get_pending contract). The
 * `photo_json IS NOT NULL` predicate is belt-and-suspenders: a pending row without
 * bytes (schema-impossible on the write path) is unscreenable and must not be served
 * — the stuck-pending prune stage is its terminal path. Returns a NAMED field
 * (portal_client._request rejects non-object JSON). Bearer-token gated.
 */
app.get("/api/internal/item-photos/pending", requireInternalToken, async (c) => {
  const limit = Math.min(Math.max(parseInt(c.req.query("limit") || "25", 10) || 25, 1), 100);
  const { results } = await c.env.DB
    .prepare(
      "SELECT id, item_state_id, photo_json, hmac, created_at FROM item_photos " +
        "WHERE status = 'pending' AND photo_json IS NOT NULL ORDER BY created_at ASC, id ASC LIMIT ?",
    )
    .bind(limit)
    .all();
  return c.json({ item_photos: results });
});

/**
 * POST /api/internal/item-photos/:id/result — apply one screening disposition.
 * Body: { status: 'clean'|'refused', box_file_id? (clean ONLY — required; the Box
 * record must already exist), detail? (refused machine reason — audit only, never bytes) }.
 *
 * ONE atomic batch (W4): photo_ref flip on the owning item state + the item_photos
 * disposition (status + **photo_json=NULL — delete-on-screen, the bytes leave D1** +
 * box_file_id + screened_at) + the changes()-gated audit row. The item state's
 * COMPLETION STATUS is never touched — a refused photo means evidence refused, NOT
 * work not done (the item completion stands; refused vacates the one-photo slot so
 * the crew can retry).
 *
 * Idempotent: a re-post for an already-screened / unknown row returns
 * { ok:true, found:false } with NO writes (the status='pending' guards make the
 * batch a structural no-op even under a lost race between the SELECT and the batch,
 * and a LATE re-post can never clobber a newer retry's 'pending:<newid>' ref — the
 * photo_ref UPDATE resolves its target through the still-pending row). Bearer-token gated.
 */
app.post("/api/internal/item-photos/:id/result", requireInternalToken, async (c) => {
  const photoId = parseInt(c.req.param("id"), 10);
  if (isNaN(photoId) || photoId < 1) return c.json({ error: "invalid_id" }, 400);
  let body: Record<string, unknown>;
  try {
    body = await c.req.json();
  } catch {
    return c.json({ error: "bad_request" }, 400);
  }
  if (typeof body !== "object" || (body as unknown) === null || Array.isArray(body)) {
    return c.json({ error: "bad_request" }, 400);
  }
  const status = typeof body.status === "string" && ITEM_PHOTO_RESULT_STATUSES.has(body.status)
    ? (body.status as "clean" | "refused")
    : "";
  if (!status) return c.json({ error: "invalid_result", detail: "status" }, 400);
  const boxFileId =
    typeof body.box_file_id === "string" && body.box_file_id ? body.box_file_id.slice(0, 200) : null;
  // detail: the refused machine reason (e.g. "L2:unreadable:OSError") — bounded, audit-only.
  const detail = typeof body.detail === "string" && body.detail ? body.detail.slice(0, 200) : null;
  // Tight contract (Invariant 2 — daemon input is untrusted too): clean MUST name the Box
  // record it just filed; refused must NOT carry one (a refused photo is never filed).
  if (status === "clean" && !boxFileId) {
    return c.json({ error: "invalid_result", detail: "box_file_id_required" }, 400);
  }
  if (status === "refused" && boxFileId) {
    return c.json({ error: "invalid_result", detail: "box_file_id_forbidden" }, 400);
  }

  const row = await c.env.DB
    .prepare("SELECT id, item_state_id, status FROM item_photos WHERE id = ?1")
    .bind(photoId)
    .first<{ id: number; item_state_id: number; status: string }>();
  // Unknown OR already-screened → idempotent no-op (mark_filed's found=false semantics: a
  // re-post after a lost ack — or a row the prune already deleted — is benign, never an error).
  if (!row || row.status !== "pending") {
    return c.json({ ok: true, found: false, status: row?.status ?? null });
  }

  // (W4) ONE atomic batch. Statement order is load-bearing:
  //   1. photo_ref flip FIRST — its subselect requires the item_photos row to still be
  //      'pending', so a duplicate application (lost race past the SELECT above) resolves to
  //      no target row and cannot clobber a newer retry's 'pending:<newid>' stamp.
  //   2. the disposition UPDATE (guarded status='pending') — flips status, NULLs photo_json
  //      (DELETE-ON-SCREEN), stamps box_file_id + screened_at.
  //   3. the changes()=1-gated audit row, directly after the mutation it describes.
  const res = await c.env.DB.batch([
    c.env.DB
      .prepare(
        "UPDATE checklist_item_states SET photo_ref = ?1 WHERE id = " +
          "(SELECT item_state_id FROM item_photos WHERE id = ?2 AND status = 'pending')",
      )
      .bind(`${status}:${photoId}`, photoId),
    c.env.DB
      .prepare(
        "UPDATE item_photos SET status = ?1, photo_json = NULL, box_file_id = ?2, " +
          "screened_at = unixepoch() WHERE id = ?3 AND status = 'pending'",
      )
      .bind(status, boxFileId, photoId),
    auditStmtIfChanged(c, "portal_poll", "checklist_item_photo_result", String(photoId), {
      item_photo_id: photoId,
      item_state_id: row.item_state_id,
      status,
      box_file_id: boxFileId,
      detail,
    }),
  ]);
  return c.json({ ok: true, found: (res[1]?.meta?.changes ?? 0) > 0 });
});

// ── DR-photo-pool Slice 2: the daily-pool photo screening queue (Mac portal_poll pass) ──
// The daily_photo_pool (migration 0037) twin of the item-photo queue above — the SAME
// requireInternalToken middleware instance, the same Option-D posture (record-only, no
// serving route, DELETE-ON-SCREEN), the same found:false idempotency. The Mac's
// _service_daily_photos pass GETs the pending queue, verifies each row's HMAC
// (shared/portal_hmac.verify_daily_photo — "daily_photo:v1"), runs the byte-identical
// §34 photo_screen pipeline, files a CLEAN sanitized re-encode to Box
// (ITS Photos/daily/<job_id>/<work_date>/), and POSTs the disposition back here.
const DAILY_PHOTO_RESULT_STATUSES = new Set(["clean", "refused"]);

/** Validate a thumb_b64 candidate END-TO-END at WRITE time: base64 shape + length-validity
 *  (B64_RE alone admits len ≡ 1 mod 4 — e.g. "AAAAA" — which atob() throws on at SERVE time,
 *  and a stored bad thumb is a PERMANENT per-photo 500: the status='pending' guard makes the
 *  disposition unrepeatable; adversarial review 2026-08-13, MEDIUM), decoded-size bound, and
 *  JPEG magic (make_thumbnail only ever emits JPEG — anything else is not a thumb we made).
 *  Returns the machine reason, or null when valid. */
function thumbB64Problem(thumb: string): string | null {
  if (!B64_RE.test(thumb) || thumb.length % 4 !== 0) return "thumb_invalid";
  if (b64DecodedLen(thumb) > THUMB_MAX_BYTES) return "thumb_too_large";
  let bytes: string;
  try {
    bytes = atob(thumb);
  } catch {
    return "thumb_invalid";
  }
  if (bytes.length > THUMB_MAX_BYTES) return "thumb_too_large";
  if (bytes.length < 3 || bytes.charCodeAt(0) !== 0xff || bytes.charCodeAt(1) !== 0xd8 || bytes.charCodeAt(2) !== 0xff) {
    return "thumb_not_jpeg";
  }
  return null;
}

/**
 * GET /api/internal/daily-photos/pending — the unscreened pool queue, oldest-first.
 * Serves claimed AND unclaimed pending rows alike (a claim changes ownership, not
 * screening need — and screening claimed rows FIRST is what makes the pass-before-drain
 * ordering pay off). Each row carries the VERBATIM stored photo_json + the Worker's
 * HMAC plus the HMAC-covered (job_id, work_date) tuple the Mac needs both to verify
 * and to derive the Box filing path. `photo_json IS NOT NULL` is belt-and-suspenders
 * (a pending row without bytes is unscreenable — the prune stage is its terminal path).
 */
app.get("/api/internal/daily-photos/pending", requireInternalToken, async (c) => {
  const limit = Math.min(Math.max(parseInt(c.req.query("limit") || "25", 10) || 25, 1), 100);
  const { results } = await c.env.DB
    .prepare(
      "SELECT id, job_id, work_date, photo_json, hmac, created_at FROM daily_photo_pool " +
        "WHERE status = 'pending' AND photo_json IS NOT NULL ORDER BY created_at ASC, id ASC LIMIT ?",
    )
    .bind(limit)
    .all();
  return c.json({ daily_photos: results });
});

/**
 * POST /api/internal/daily-photos/:id/result — apply one screening disposition.
 * Body: { status: 'clean'|'refused', box_file_id? (clean ONLY — required; the Box
 * record must already exist), detail? (refused machine reason — audit only, never bytes) }.
 *
 * ONE atomic batch (W4): the disposition UPDATE (status + **photo_json=NULL —
 * delete-on-screen, the bytes leave D1** + box_file_id + screened_at, guarded
 * status='pending') + the changes()-gated audit row. Unlike the item-photo twin
 * there is NO sibling ref flip — pool rows self-describe their status (the SPA's
 * own-photos chips and the /pending claim manifest read it directly). A CLAIM is
 * never touched: a claimed row that screens becomes the submission's byte-free
 * photo manifest (clean) or its refused marker.
 *
 * Idempotent: a re-post for an already-screened / unknown row returns
 * { ok:true, found:false } with NO writes (the status='pending' guard makes the
 * batch a structural no-op even under a lost race between the SELECT and the
 * batch). Bearer-token gated (requireInternalToken — the same middleware instance).
 */
app.post("/api/internal/daily-photos/:id/result", requireInternalToken, async (c) => {
  const photoId = parseInt(c.req.param("id"), 10);
  if (isNaN(photoId) || photoId < 1) return c.json({ error: "invalid_id" }, 400);
  let body: Record<string, unknown>;
  try {
    body = await c.req.json();
  } catch {
    return c.json({ error: "bad_request" }, 400);
  }
  if (typeof body !== "object" || (body as unknown) === null || Array.isArray(body)) {
    return c.json({ error: "bad_request" }, 400);
  }
  const status = typeof body.status === "string" && DAILY_PHOTO_RESULT_STATUSES.has(body.status)
    ? (body.status as "clean" | "refused")
    : "";
  if (!status) return c.json({ error: "invalid_result", detail: "status" }, 400);
  const boxFileId =
    typeof body.box_file_id === "string" && body.box_file_id ? body.box_file_id.slice(0, 200) : null;
  // detail: the refused machine reason (e.g. "L2:unreadable:OSError") — bounded, audit-only.
  const detail = typeof body.detail === "string" && body.detail ? body.detail.slice(0, 200) : null;
  // Tight contract (Invariant 2 — daemon input is untrusted too): clean MUST name the Box
  // record it just filed; refused must NOT carry one (a refused photo is never filed).
  if (status === "clean" && !boxFileId) {
    return c.json({ error: "invalid_result", detail: "box_file_id_required" }, 400);
  }
  if (status === "refused" && boxFileId) {
    return c.json({ error: "invalid_result", detail: "box_file_id_forbidden" }, 400);
  }
  // thumb_b64 (0074): OPTIONAL on clean — a small screened thumbnail derived from the §34 clean
  // re-encode; FORBIDDEN on refused (a refused photo leaves no image trace, same shape as the
  // box_file_id contract above). Bounded + base64-shape-checked (Invariant 2); an ABSENT thumb is
  // fine (older Mac builds / thumbnailing failure degrade to the thumbless card, never an error).
  const thumbB64 = typeof body.thumb_b64 === "string" && body.thumb_b64 ? body.thumb_b64 : null;
  if (thumbB64 !== null) {
    if (status === "refused") return c.json({ error: "invalid_result", detail: "thumb_forbidden" }, 400);
    const thumbErr = thumbB64Problem(thumbB64);
    if (thumbErr) return c.json({ error: "invalid_result", detail: thumbErr }, 400);
  }

  const row = await c.env.DB
    .prepare("SELECT id, job_id, work_date, status FROM daily_photo_pool WHERE id = ?1")
    .bind(photoId)
    .first<{ id: number; job_id: string; work_date: string; status: string }>();
  // Unknown OR already-screened → idempotent no-op (mark_filed's found=false semantics: a
  // re-post after a lost ack — or a row the prune already deleted — is benign, never an error).
  if (!row || row.status !== "pending") {
    return c.json({ ok: true, found: false, status: row?.status ?? null });
  }

  // (W4) ONE atomic batch: the disposition UPDATE (guarded status='pending' — a lost race
  // past the SELECT above is a structural no-op) + the changes()=1-gated audit row.
  const res = await c.env.DB.batch([
    c.env.DB
      .prepare(
        "UPDATE daily_photo_pool SET status = ?1, photo_json = NULL, box_file_id = ?2, " +
          "thumb_b64 = ?4, screened_at = unixepoch() WHERE id = ?3 AND status = 'pending'",
      )
      .bind(status, boxFileId, photoId, thumbB64),
    auditStmtIfChanged(c, "portal_poll", "daily_photo_result", row.job_id, {
      daily_photo_id: photoId,
      job_id: row.job_id,
      work_date: row.work_date,
      status,
      box_file_id: boxFileId,
      detail,
    }),
  ]);
  return c.json({ ok: true, found: (res[0]?.meta?.changes ?? 0) > 0 });
});

/**
 * POST /api/internal/daily-photos/register — the SITE-PHOTOS BRIDGE (0074, Track B 2026-08-13).
 *
 * The daily report's INLINE `site_photos` ride the submission payload, are §34-screened by the
 * Mac's intake, filed to Box under the submission's own folder, and embedded in the daily PDF —
 * but they never had a pool row, so the WPR photo picker (which reads ONLY daily_photo_pool)
 * structurally could not offer them. After filing, portal_poll POSTs each clean site photo here
 * and the row becomes WPR-offerable exactly like a screened additional_photos row.
 *
 * TRUST SHAPE (Invariant 2 — daemon input is untrusted too): the body names ONLY the submission
 * uuid and per-photo {box_file_id, caption?, thumb_b64?}. job_id / work_date / uploaded_by are
 * derived SERVER-SIDE from the submissions row — the daemon cannot place a photo on a job or day
 * its submission does not belong to. Unknown submission → 404 (nothing stored).
 *
 * The 1..8 bound is PER CALL (the Mac's photo_screen caps site photos at 8 per submission at
 * the source; repeated calls with fresh box_file_ids are bearer-trusted like box_file_id itself).
 *
 * IDEMPOTENT by (claimed_by_submission, box_file_id): intake re-runs re-upload the same
 * deterministic Box filenames as new VERSIONS of the SAME file ids, so a replay's INSERTs are
 * structural no-ops (guarded WHERE NOT EXISTS; the changes()-gated audit stays silent too).
 *
 * Rows are born status='clean' (their §34 screening already happened at intake), photo_json NULL
 * (no bytes, ever — only the thumb), CLAIMED by their submission (prune-immune like any claimed
 * manifest; they die only via the orphan-claim rule when the submission row itself goes), with
 * origin='site_photos' and the hmac sentinel 'registered:v1' (nothing reads hmac off non-pending
 * rows — the pending screening queue serves status='pending' only, which these never are).
 */
app.post("/api/internal/daily-photos/register", requireInternalToken, async (c) => {
  let body: Record<string, unknown>;
  try {
    body = await c.req.json();
  } catch {
    return c.json({ error: "bad_request" }, 400);
  }
  if (typeof body !== "object" || (body as unknown) === null || Array.isArray(body)) {
    return c.json({ error: "bad_request" }, 400);
  }
  const submissionUuid =
    typeof body.submission_uuid === "string" && body.submission_uuid.length > 0 && body.submission_uuid.length <= 64
      ? body.submission_uuid
      : "";
  if (!submissionUuid) return c.json({ error: "invalid_register", detail: "submission_uuid" }, 400);
  const photosRaw = body.photos;
  // 1..8 entries — MAX_PHOTOS_PER_SUBMISSION mirror (photo_screen.py); an empty register is a
  // caller bug, not a no-op to swallow.
  if (!Array.isArray(photosRaw) || photosRaw.length < 1 || photosRaw.length > 8) {
    return c.json({ error: "invalid_register", detail: "photos" }, 400);
  }
  type RegPhoto = { box_file_id: string; caption: string | null; thumb_b64: string | null };
  const photos: RegPhoto[] = [];
  for (const item of photosRaw) {
    if (typeof item !== "object" || item === null || Array.isArray(item)) {
      return c.json({ error: "invalid_register", detail: "photo_shape" }, 400);
    }
    const rec = item as Record<string, unknown>;
    const boxFileId =
      typeof rec.box_file_id === "string" && rec.box_file_id.length > 0 && rec.box_file_id.length <= 200
        ? rec.box_file_id
        : "";
    if (!boxFileId) return c.json({ error: "invalid_register", detail: "box_file_id" }, 400);
    const caption = typeof rec.caption === "string" && rec.caption ? rec.caption.slice(0, 300) : null;
    const thumb = typeof rec.thumb_b64 === "string" && rec.thumb_b64 ? rec.thumb_b64 : null;
    if (thumb !== null) {
      const thumbErr = thumbB64Problem(thumb);
      if (thumbErr) return c.json({ error: "invalid_register", detail: thumbErr }, 400);
    }
    photos.push({ box_file_id: boxFileId, caption, thumb_b64: thumb });
  }

  // Server-derived placement: the submission row is the authority for job/date/actor.
  const sub = await c.env.DB
    .prepare("SELECT job_id, work_date, actor_username FROM submissions WHERE submission_uuid = ?1")
    .bind(submissionUuid)
    .first<{ job_id: string; work_date: string; actor_username: string }>();
  if (!sub) return c.json({ error: "unknown_submission" }, 404);

  // ONE batch: per-photo guarded idempotent INSERT, each with its changes()-gated audit row (W4).
  const stmts: D1PreparedStatement[] = [];
  for (const ph of photos) {
    stmts.push(
      c.env.DB
        .prepare(
          "INSERT INTO daily_photo_pool " +
            "(job_id, work_date, uploaded_by, status, photo_json, hmac, box_file_id, " +
            " screened_at, claimed_by_submission, origin, caption, thumb_b64) " +
            "SELECT ?1, ?2, ?3, 'clean', NULL, 'registered:v1', ?4, unixepoch(), ?5, 'site_photos', ?6, ?7 " +
            "WHERE NOT EXISTS (SELECT 1 FROM daily_photo_pool " +
            "                   WHERE claimed_by_submission = ?5 AND box_file_id = ?4)",
        )
        .bind(sub.job_id, sub.work_date, sub.actor_username, ph.box_file_id, submissionUuid, ph.caption, ph.thumb_b64),
    );
    stmts.push(
      auditStmtIfChanged(c, "portal_poll", "daily_photo_register", sub.job_id, {
        submission_uuid: submissionUuid,
        box_file_id: ph.box_file_id,
        job_id: sub.job_id,
        work_date: sub.work_date,
      }),
    );
  }
  const res = await c.env.DB.batch(stmts);
  let registered = 0;
  for (let i = 0; i < res.length; i += 2) registered += (res[i]?.meta?.changes ?? 0) > 0 ? 1 : 0;
  return c.json({ ok: true, registered, skipped: photos.length - registered });
});

/**
 * POST /api/internal/sync — full-replace sync of the active-job set from the Mac
 * side (portal_poll reads ITS_Active_Jobs and POSTs the COMPLETE set each cycle).
 * Bearer-token gated. This is the write-leg counterpart to GET /api/jobs (which
 * the SPA reads): Smartsheet is the source of truth, D1 is the dropdown cache.
 *
 * Body: { jobs: [{ job_id, project_name, active, address }] } — the complete ITS_Active_Jobs
 * set, each row carrying its own active flag (1/0). The payload is AUTHORITATIVE:
 * any D1 job_id ABSENT from it is deactivated (active=0) so a job removed/archived
 * in Smartsheet drops off the dropdown. We never DELETE (submissions reference
 * job_id — deactivate, don't orphan). Upserts + the single reconcile run in ONE
 * atomic D1 batch.
 *
 * INVARIANT 1: still ZERO external transmission — this only writes D1; the Mac side
 * initiated the request, the Worker sends nothing outward. INVARIANT 2: every row
 * is type-checked + length-bounded, all D1 access is parameter-bound, the batch is
 * size-capped, and an EMPTY payload is rejected (it would otherwise wipe the whole
 * dropdown — a Smartsheet read miss on the Mac side must never reach here as []).
 */
app.post("/api/internal/sync", requireInternalToken, async (c) => {
  let body: { jobs?: unknown };
  try {
    body = await c.req.json();
  } catch {
    return c.json({ error: "bad_request" }, 400);
  }
  // JSON `null`/arrays/scalars PARSE fine but aren't objects; dereferencing body.x on
  // them threw → bare 500 (audit #1). Require a plain object (the `as unknown` cast
  // dodges the no-overlap check on the typed body var).
  if (typeof body !== "object" || (body as unknown) === null || Array.isArray(body)) {
    return c.json({ error: "bad_request" }, 400);
  }
  const raw = body.jobs;
  if (!Array.isArray(raw)) return c.json({ error: "invalid_jobs" }, 400);
  if (raw.length === 0) return c.json({ error: "empty_jobs" }, 400); // never wipe the dropdown
  if (raw.length > 5000) return c.json({ error: "too_many_jobs" }, 413);

  // Validate + normalize every row up front; reject the WHOLE batch on any bad row
  // (a partial sync would silently desync the dropdown).
  const jobs: { job_id: string; project_name: string; active: number; address: string }[] = [];
  const seen = new Set<string>();
  for (const r of raw) {
    if (typeof r !== "object" || r === null) return c.json({ error: "invalid_row" }, 400);
    const row = r as Record<string, unknown>;
    const job_id = typeof row.job_id === "string" ? row.job_id : "";
    const project_name = typeof row.project_name === "string" ? row.project_name : "";
    const active = row.active === 1 || row.active === true ? 1 : 0;
    // `address` (C1): OPTIONAL — a Mac daemon that predates it omits the field (→ ""). Bound it like
    // the other free-text columns; it only auto-fills the subcontract builder's Site address, so a
    // blank is fine (the field stays operator-editable).
    const address = typeof row.address === "string" ? row.address : "";
    if (!job_id || job_id.length > 64 || !project_name || project_name.length > 256 || address.length > MAX_ADDRESS) {
      return c.json({ error: "invalid_row" }, 400);
    }
    if (seen.has(job_id)) return c.json({ error: "duplicate_job_id" }, 400);
    seen.add(job_id);
    jobs.push({ job_id, project_name, active, address });
  }

  // P2.5 canonical-aware pre-pass: once the mirror daemon promotes a portal job, the SAFETY sheet
  // assigns it a JOB-#### and list_all_jobs() pushes that JOB-#### here — but the D1 row is
  // origin='portal' keyed by the TYPED job_id, so a naive ON CONFLICT(job_id) would MISS and INSERT
  // a duplicate origin='smartsheet' row (a persistent ghost in the dropdown). Drop any pushed row
  // whose job_id equals a portal row's canonical_job_id (the safety read-back) from the UPSERT set.
  // The DEACTIVATION still uses the FULL pushed id list: those canonical JOB-####s correspond to
  // origin='portal' rows (never origin='smartsheet'), so they're inert in the smartsheet-scoped
  // sweep, and keeping them keeps the bound NOT-IN list non-empty + the sweep correct.
  const canonRows = await c.env.DB
    .prepare("SELECT canonical_job_id FROM jobs WHERE origin='portal' AND canonical_job_id IS NOT NULL")
    .all<{ canonical_job_id: string }>();
  const canonical = new Set((canonRows.results ?? []).map((r) => r.canonical_job_id));
  const toUpsert = jobs.filter((j) => !canonical.has(j.job_id));

  // One atomic batch: upsert every NON-canonical supplied row, then deactivate any active D1
  // job_id NOT in the (full) payload (the NOT-IN list is bound, never interpolated).
  const ids = jobs.map((j) => j.job_id);
  const statements = [
    ...toUpsert.map((j) =>
      c.env.DB.prepare(
        "INSERT INTO jobs (job_id, project_name, active, address) VALUES (?,?,?,?) " +
          "ON CONFLICT(job_id) DO UPDATE SET project_name=excluded.project_name, active=excluded.active, " +
          "address=excluded.address",
      ).bind(j.job_id, j.project_name, j.active, j.address),
    ),
    c.env.DB.prepare(
      // origin fence (migration 0017): only smartsheet-origin jobs participate in the
      // full-replace deactivation. A portal-CREATED job (origin='portal') is absent from the
      // Smartsheet payload until the mirror daemon promotes it, so it must never be deactivated here.
      // ORDER DEPENDENCY: migration 0017 (the `origin` column) must be live BEFORE this Worker
      // deploys, else this UPDATE 500s on an unknown column (mirror of the 0007/0009 activation rule).
      `UPDATE jobs SET active=0 WHERE active=1 AND origin='smartsheet' AND job_id NOT IN (${ids.map(() => "?").join(",")})`,
    ).bind(...ids),
  ];
  const results = await c.env.DB.batch(statements);
  const deactivated = results[results.length - 1]?.meta?.changes ?? 0;
  return c.json({ ok: true, upserted: toUpsert.length, deactivated });
});

/**
 * GET /api/internal/prune-status — the prune-observability read (GS2, unbounded-growth
 * audit Slice 2). Returns the one-row prune_meta record the scheduled daily prune UPSERTs
 * after every run (see prune.writePruneMeta / migration 0033); `{ prune: null }` when the
 * prune has never recorded a run. The Mac watchdog (Check V) consumes it: WARN when
 * last_run_at goes >48h stale, CRITICAL on failed_stages non-empty or db_size_bytes over
 * the 6 GB threshold.
 *
 * Bearer gate: requireInternalToken — the SAME token tier as GET /api/internal/pending
 * (PORTAL_INTERNAL_API_TOKEN / Keychain ITS_PORTAL_INTERNAL_TOKEN), because the consumer is
 * the same Mac-side trust domain as the poller, and prune telemetry grants no queue-drain
 * or provisioning capability beyond what that token already holds. Read-only, bounded
 * (single row by schema CHECK). Returns a NAMED field (never a bare array/scalar —
 * portal_client._request rejects non-object JSON).
 *
 * Malformed-JSON posture: counters_json falls back to {} (informational only), but an
 * unparseable failed_stages_json surfaces as ["<unparseable>"] — fail-LOUD, because a
 * corrupted failure flag must never read as a clean run downstream.
 */
app.get("/api/internal/prune-status", requireInternalToken, async (c) => {
  const row = await c.env.DB
    .prepare(
      "SELECT last_run_at, db_size_bytes, size_warn, counters_json, failed_stages_json FROM prune_meta WHERE id = 1",
    )
    .first<{
      last_run_at: number;
      db_size_bytes: number;
      size_warn: number;
      counters_json: string;
      failed_stages_json: string;
    }>();
  if (!row) return c.json({ prune: null });
  let counters: unknown = {};
  try {
    counters = JSON.parse(row.counters_json);
  } catch {
    counters = {};
  }
  let failedStages: unknown;
  try {
    failedStages = JSON.parse(row.failed_stages_json);
    if (!Array.isArray(failedStages)) failedStages = ["<unparseable>"];
  } catch {
    failedStages = ["<unparseable>"];
  }
  return c.json({
    prune: {
      last_run_at: row.last_run_at,
      db_size_bytes: row.db_size_bytes,
      size_warn: row.size_warn === 1,
      counters,
      failed_stages: failedStages,
    },
  });
});

/**
 * Field-ops job-mirror queue — /api/internal/fieldops/* (requireFieldopsToken, the mirror
 * daemon's OWN secret; privilege-separated from the portal_poll + admin tokens). P2.5 up-sync.
 *
 * GET /pending-jobs — dirty portal jobs (origin='portal' AND sync_state='pending'): the full SoR
 * payload + the version vector + cached Smartsheet row ids the daemon needs to find-or-create a row
 * in BOTH Active-Jobs sheets. Read-only; bound SQL; capped at 200 rows/cycle (the daemon drains
 * across cycles). CC arrays are returned parsed (JSON → string[]).
 */
const FIELDOPS_PENDING_CAP = 200;
app.get("/api/internal/fieldops/pending-jobs", requireFieldopsToken, async (c) => {
  const rows = await c.env.DB
    .prepare(
      `SELECT job_id, project_name, lifecycle, address,
              stakeholder_name, stakeholder_email, stakeholder_phone,
              safety_contact_name, safety_contact_email, safety_cc,
              progress_contact_name, progress_contact_email, progress_cc,
              mirror_version, safety_mirrored_version, progress_mirrored_version,
              safety_row_id, progress_row_id, canonical_job_id
         FROM jobs
        WHERE origin='portal' AND sync_state='pending'
        ORDER BY mirror_version ASC, job_id ASC
        LIMIT ?1`,
    )
    .bind(FIELDOPS_PENDING_CAP)
    .all<Record<string, unknown>>();
  const parseCcJson = (v: unknown): string[] => {
    if (typeof v !== "string" || !v) return [];
    try {
      const a = JSON.parse(v);
      return Array.isArray(a) ? a.filter((x) => typeof x === "string") : [];
    } catch {
      return [];
    }
  };
  const jobs = (rows.results ?? []).map((r) => ({
    ...r,
    safety_cc: parseCcJson(r.safety_cc),
    progress_cc: parseCcJson(r.progress_cc),
  }));
  return c.json({ jobs });
});

/**
 * POST /jobs-mark-mirrored — the daemon's per-sheet commit point. Body:
 *   { updates: [{ job_id, sheet: 'safety'|'progress', mirrored_version, row_id, canonical_job_id? }] }
 * For each update: MONOTONICALLY advance ONLY that sheet's watermark (MAX, so a stale/replayed call
 * can never regress it), cache that sheet's row_id, and — for the SAFETY sheet only — write back the
 * canonical_job_id (the sheet's read-back JOB-####, COALESCE so a null never erases it). Then flip
 * sync_state→'synced' IFF both watermarks have reached mirror_version (else it stays 'pending' and
 * the job is re-attempted next cycle — the partial-failure self-heal). One atomic batch + a single
 * summary audit row. row-set is disjoint from the down-sync (origin='portal'), so no write conflict.
 */
app.post("/api/internal/fieldops/jobs-mark-mirrored", requireFieldopsToken, async (c) => {
  let body: { updates?: unknown };
  try {
    body = await c.req.json();
  } catch {
    return c.json({ error: "bad_request" }, 400);
  }
  if (typeof body !== "object" || (body as unknown) === null || Array.isArray(body)) {
    return c.json({ error: "bad_request" }, 400);
  }
  const raw = body.updates;
  if (!Array.isArray(raw)) return c.json({ error: "invalid_updates" }, 400);
  if (raw.length === 0) return c.json({ error: "empty_updates" }, 400);
  if (raw.length > FIELDOPS_PENDING_CAP) return c.json({ error: "too_many_updates" }, 413);

  const statements = [];
  const touched: string[] = [];
  for (const u of raw) {
    if (typeof u !== "object" || u === null || Array.isArray(u)) return c.json({ error: "invalid_update" }, 400);
    const row = u as Record<string, unknown>;
    const jobId = typeof row.job_id === "string" ? row.job_id : "";
    const sheet = row.sheet === "safety" || row.sheet === "progress" ? row.sheet : "";
    const version = typeof row.mirrored_version === "number" && Number.isInteger(row.mirrored_version) ? row.mirrored_version : -1;
    const rowId = typeof row.row_id === "number" && Number.isInteger(row.row_id) ? row.row_id : null;
    const canonical = typeof row.canonical_job_id === "string" && row.canonical_job_id ? row.canonical_job_id : null;
    if (!jobId || jobId.length > 64 || !sheet || version < 0 || rowId === null) {
      return c.json({ error: "invalid_update" }, 400);
    }
    if (sheet === "safety") {
      statements.push(
        c.env.DB
          .prepare(
            "UPDATE jobs SET safety_mirrored_version=MAX(safety_mirrored_version, ?2), safety_row_id=?3, " +
              "canonical_job_id=COALESCE(?4, canonical_job_id) WHERE job_id=?1 AND origin='portal'",
          )
          .bind(jobId, version, rowId, canonical),
      );
    } else {
      statements.push(
        c.env.DB
          .prepare(
            "UPDATE jobs SET progress_mirrored_version=MAX(progress_mirrored_version, ?2), progress_row_id=?3 " +
              "WHERE job_id=?1 AND origin='portal'",
          )
          .bind(jobId, version, rowId),
      );
    }
    // Flip the dirty flag only when BOTH sheets have caught up to the current mirror_version.
    statements.push(
      c.env.DB
        .prepare(
          "UPDATE jobs SET sync_state=CASE WHEN safety_mirrored_version>=mirror_version " +
            "AND progress_mirrored_version>=mirror_version THEN 'synced' ELSE 'pending' END " +
            "WHERE job_id=?1 AND origin='portal'",
        )
        .bind(jobId),
    );
    touched.push(`${jobId}:${sheet}`);
  }
  // One summary audit row for the whole batch (system actor — token-gated daemon, no session).
  statements.push(
    c.env.DB
      .prepare("INSERT INTO audit_log (actor_username, action, target_username, detail) VALUES (?1,?2,?3,?4)")
      .bind("system:fieldops_sync", "jobs_mark_mirrored", "", JSON.stringify({ count: touched.length, touched: touched.slice(0, 50) })),
  );
  await c.env.DB.batch(statements);
  return c.json({ ok: true, updated: raw.length });
});

/**
 * GET /hours-pending — unmirrored crew time entries (P7 Hours Log up-sync, Track 2). Each row
 * carries everything the daemon needs to find-or-create the job's per-job "Hours Log" sheet
 * (progress workspace) and upsert/supersede the entry row: the entry uuid (find-or-create key +
 * amend target), its job's project_name (folder key), the hours, the task description (LEFT JOIN
 * task_assignments via t.task_id, JOB-SCOPED with `AND ta.job_id = t.job_id` so a mis-scoped
 * task_id can never surface another job's task text — read-site defense-in-depth over the two
 * writers that already validate task↔job at write time — NULL when the entry references no task),
 * the amend link, the
 * server record time, and the DISPLAY-NAME-ONLY personnel name (never a username — House Reflex
 * §5). The wall-clock work_started_at/_ended_at are NO LONGER projected (the portal daily-report
 * form never populates them; they stay on time_entries for the rollup/personnel views). Read-only;
 * bound SQL; capped; job-ordered so the daemon batches per job. An entry whose job row is missing
 * (data anomaly) is simply not returned (INNER JOIN) — it cannot be foldered, and it re-appears the
 * moment the job row exists.
 */
/**
 * ── Track 6 job archive: the daemon's queue + commit point ──────────────────────────────────────
 *
 * GET /archive-pending — jobs awaiting relocation. Deliberately its OWN queue rather than riding
 * the job-dirty list: `jobs-mark-mirrored` flips sync_state to 'synced' the moment both sheets
 * catch up, which is precisely WHY the pre-Track-6 archive move "did not auto-retry" — an
 * unrelated mirror success silenced it. A dedicated queue keeps re-serving a job until its archive
 * reaches a terminal state, and cannot be quieted by anything else.
 *
 * The cap is deliberately small: each row costs SIX external API sequences across two systems, so
 * a 200-row page would blow the daemon's cycle budget.
 */
const FIELDOPS_ARCHIVE_CAP = 25;

app.get("/api/internal/fieldops/archive-pending", requireFieldopsToken, async (c) => {
  const rows = await c.env.DB
    .prepare(
      `SELECT job_id, project_name, job_no, archive_folder_key, archive_direction,
              archive_state, archive_attempts, archive_requested_at
         FROM jobs
        WHERE origin='portal' AND archive_state IN ('requested','in_progress')
        ORDER BY archive_requested_at ASC, job_id ASC
        LIMIT ?1`,
    )
    .bind(FIELDOPS_ARCHIVE_CAP)
    .all<Record<string, unknown>>();
  return c.json({ jobs: rows.results ?? [] });
});

/**
 * GET /archive-health — the OBSERVABILITY read, for watchdog Check X. Strictly read-only.
 *
 * Why this is not just `archive-pending`. That route is the daemon's WORK queue, so it serves
 * only `requested` / `in_progress` — which makes it structurally blind to exactly the state an
 * operator most needs to hear about. `partial` and `failed` are TERMINAL for the daemon: a job
 * that reaches one leaves the queue and resumes only when a human presses "Try again" in the
 * portal. So a job can sit half-relocated, its folders split across Smartsheet and Box, with no
 * queue entry, no retry, and (before Check X) no detector. This route deliberately widens the
 * state filter to include those two, so the watchdog can see a stopped archive standing still.
 *
 * It also serves `requested` / `in_progress` — the other half of #25, where a request simply
 * never gets picked up (the 2026-08-10 incident: `JOB-000030` sat at `requested` with
 * `archive_attempts=0` for hours because the pass's gate was off, while the portal showed a
 * green "Waiting for the office Mac to pick this up").
 *
 * MUTATION-FREE by construction, and it must stay that way: it is a health probe running on the
 * watchdog's schedule, not the daemon's, and anything it wrote would be a write nobody asked
 * for. `archive-pending` remains the ONLY route the archive pass drains.
 *
 * Ordered oldest-request-first so a truncated page still shows the worst offenders. The cap is
 * generous relative to `archive-pending`'s 25 because a row here costs one SELECT, not six
 * external API sequences — but it is still bounded, and Check X reports the count it received.
 */
const FIELDOPS_ARCHIVE_HEALTH_CAP = 200;

app.get("/api/internal/fieldops/archive-health", requireFieldopsToken, async (c) => {
  const rows = await c.env.DB
    .prepare(
      `SELECT job_id, project_name, job_no, archive_folder_key, archive_direction,
              archive_state, archive_attempts, archive_requested_at
         FROM jobs
        WHERE origin='portal'
          AND archive_state IN ('requested','in_progress','partial','failed')
        ORDER BY archive_requested_at ASC, job_id ASC
        LIMIT ?1`,
    )
    .bind(FIELDOPS_ARCHIVE_HEALTH_CAP)
    .all<Record<string, unknown>>();
  return c.json({ jobs: rows.results ?? [] });
});

/**
 * POST /job-archive-progress — the pass's commit point. Body:
 *   { updates: [{ job_id, direction, state, containers?, note? }, …] }
 *
 * FORWARD-ONLY by construction. Every UPDATE carries
 *   AND archive_state IN ('requested','in_progress') AND archive_direction = ?
 * so a late or replayed post from a previous cycle can never resurrect a completed archive, nor
 * apply an ARCHIVE result to a job the operator has since flipped to un-archive. A row that no
 * longer matches is reported in `skipped` rather than failing the batch — one stale member must
 * not discard the other 24 genuine results.
 *
 * Validate-ALL-then-execute (the jobs-mark-mirrored contract): a malformed member anywhere rejects
 * the whole request before a single statement runs, so a partial application is impossible.
 *
 * A COMPLETED UN-ARCHIVE resets the record to neutral ('none' / '' / NULLs). The audit_log keeps
 * the history; the row goes back to being an ordinary job — and, importantly, becomes prunable
 * again, which is the behaviour prune.ts's archive fence assumes.
 */
const ARCHIVE_PROGRESS_STATES = new Set(["in_progress", "complete", "partial", "failed"]);
const ARCHIVE_DETAIL_MAX = 4000;

app.post("/api/internal/fieldops/job-archive-progress", requireFieldopsToken, async (c) => {
  let body: { updates?: unknown };
  try {
    body = await c.req.json();
  } catch {
    return c.json({ error: "bad_request" }, 400);
  }
  if (typeof body !== "object" || (body as unknown) === null || Array.isArray(body)) {
    return c.json({ error: "bad_request" }, 400);
  }
  const raw = body.updates;
  if (!Array.isArray(raw)) return c.json({ error: "invalid_updates" }, 400);
  if (raw.length === 0) return c.json({ error: "empty_updates" }, 400);
  if (raw.length > FIELDOPS_ARCHIVE_CAP) return c.json({ error: "too_many_updates" }, 413);

  // ── validate ALL first ──
  interface ArchiveUpdate { jobId: string; direction: string; state: string; detail: string }
  const parsed: ArchiveUpdate[] = [];
  for (const u of raw) {
    if (typeof u !== "object" || u === null || Array.isArray(u)) return c.json({ error: "invalid_update" }, 400);
    const row = u as Record<string, unknown>;
    const jobId = typeof row.job_id === "string" ? row.job_id : "";
    const direction = row.direction === "archive" || row.direction === "unarchive" ? row.direction : "";
    const state = typeof row.state === "string" ? row.state : "";
    if (!jobId || jobId.length > 64 || !direction) return c.json({ error: "invalid_update" }, 400);
    // 'requested' is BROWSER-only — the daemon may never raise a request, only advance one.
    if (!ARCHIVE_PROGRESS_STATES.has(state)) return c.json({ error: "invalid_archive_state" }, 400);
    const detail = row.containers === undefined ? "" : JSON.stringify(row.containers);
    if (detail.length > ARCHIVE_DETAIL_MAX) return c.json({ error: "invalid_update" }, 400);
    parsed.push({ jobId, direction, state, detail });
  }

  // ── then execute ──
  const statements = [];
  for (const u of parsed) {
    statements.push(
      c.env.DB
        .prepare(
          "UPDATE jobs SET archive_state=?2, archive_detail=?3, " +
            "archive_attempts = archive_attempts + (CASE WHEN ?2 IN ('partial','failed') THEN 1 ELSE 0 END), " +
            "archive_completed_at = (CASE WHEN ?2='complete' THEN unixepoch() ELSE archive_completed_at END) " +
            "WHERE job_id=?1 AND origin='portal' " +
            "AND archive_state IN ('requested','in_progress') AND archive_direction=?4",
        )
        .bind(u.jobId, u.state, u.detail, u.direction),
    );
    if (u.state === "complete" && u.direction === "unarchive") {
      // The job is back in the live tree — clear the record so it behaves like any other job
      // (and becomes prunable again, per the prune.ts archive fence).
      statements.push(
        c.env.DB
          .prepare(
            "UPDATE jobs SET archive_state='none', archive_direction='', archive_requested_at=NULL, " +
              "archive_completed_at=NULL, archive_attempts=0, archive_detail='', archive_folder_key='' " +
              "WHERE job_id=?1 AND origin='portal' AND archive_state='complete' AND archive_direction='unarchive'",
          )
          .bind(u.jobId),
      );
    }
  }
  statements.push(
    c.env.DB
      .prepare("INSERT INTO audit_log (actor_username, action, target_username, detail) VALUES (?1,?2,?3,?4)")
      .bind(
        "system:fieldops_sync",
        "job_archive_progress",
        "",
        JSON.stringify({ count: parsed.length, jobs: parsed.slice(0, 25).map((u) => `${u.jobId}:${u.state}`) }),
      ),
  );

  const res = await c.env.DB.batch(statements);

  // Report which members matched nothing so the daemon can distinguish "applied" from "the
  // operator moved this row out from under me" without a second round trip.
  const skipped: string[] = [];
  let i = 0;
  for (const u of parsed) {
    const changed = res[i].meta.changes ?? 0;
    if (changed === 0) skipped.push(u.jobId);
    i += u.state === "complete" && u.direction === "unarchive" ? 2 : 1;
  }
  return c.json({ ok: true, updated: parsed.length - skipped.length, skipped });
});

const FIELDOPS_HOURS_CAP = 200;
app.get("/api/internal/fieldops/hours-pending", requireFieldopsToken, async (c) => {
  const rows = await c.env.DB
    .prepare(
      `SELECT t.uuid, t.job_id, j.project_name,
              t.hours, t.notes, ta.description AS task,
              t.amends_uuid, t.created_at, p.name AS personnel_name
         FROM time_entries t
         JOIN jobs j ON j.job_id = t.job_id
         LEFT JOIN personnel p ON p.id = t.personnel_id
         LEFT JOIN task_assignments ta ON ta.id = t.task_id AND ta.job_id = t.job_id
        WHERE t.mirrored_at IS NULL
        ORDER BY t.job_id ASC, t.created_at ASC
        LIMIT ?1`,
    )
    .bind(FIELDOPS_HOURS_CAP)
    .all<Record<string, unknown>>();
  return c.json({ entries: rows.results ?? [] });
});

/**
 * POST /hours-mark-mirrored — the hours pass's commit point. Body: { uuids: [uuid, …] } — the
 * entries whose per-job Hours Log row the daemon confirmed this cycle. Each stamps
 * mirrored_at = unixepoch() IFF still NULL (idempotent: a replay/re-mirror is a no-op, never a
 * regress). One atomic batch + a single summary audit row (system actor — token-gated daemon, no
 * session). Crash-safe: a crash before this leaves the entry unmirrored → re-upserted next cycle
 * (the sheet find-or-create by Entry UUID no-ops) → stamped.
 */
app.post("/api/internal/fieldops/hours-mark-mirrored", requireFieldopsToken, async (c) => {
  let body: { uuids?: unknown };
  try {
    body = await c.req.json();
  } catch {
    return c.json({ error: "bad_request" }, 400);
  }
  if (typeof body !== "object" || (body as unknown) === null || Array.isArray(body)) {
    return c.json({ error: "bad_request" }, 400);
  }
  const raw = body.uuids;
  if (!Array.isArray(raw)) return c.json({ error: "invalid_uuids" }, 400);
  if (raw.length === 0) return c.json({ error: "empty_uuids" }, 400);
  if (raw.length > FIELDOPS_HOURS_CAP) return c.json({ error: "too_many_uuids" }, 413);

  const uuids: string[] = [];
  for (const u of raw) {
    if (typeof u !== "string" || !u || u.length > 64) return c.json({ error: "invalid_uuid" }, 400);
    uuids.push(u);
  }
  const statements = uuids.map((uuid) =>
    c.env.DB
      .prepare("UPDATE time_entries SET mirrored_at = unixepoch() WHERE uuid = ?1 AND mirrored_at IS NULL")
      .bind(uuid),
  );
  statements.push(
    c.env.DB
      .prepare("INSERT INTO audit_log (actor_username, action, target_username, detail) VALUES (?1,?2,?3,?4)")
      .bind(
        "system:fieldops_sync",
        "hours_mark_mirrored",
        "",
        JSON.stringify({ count: uuids.length, uuids: uuids.slice(0, 50) }),
      ),
  );
  await c.env.DB.batch(statements);
  return c.json({ ok: true, updated: uuids.length });
});

/**
 * GET /equipment-snapshot — the CURRENT on-active-job equipment across all active jobs (P7
 * Slice 2, Equipment Status & Location tracker). A SNAPSHOT, not an event drain: it re-projects
 * the live state every cycle, so there is NO watermark and NO mark-mirrored companion (unlike the
 * job / hours queues). One object per equipment whose LATEST `equipment_location` sits on an
 * ACTIVE job:
 *   - latest location per equipment = ROW_NUMBER() OVER (PARTITION BY equipment_id
 *     ORDER BY recorded_at DESC, id DESC) → rn = 1 (the same window the equipment tab uses);
 *   - INNER JOIN equipment (active = 1) for the denormalized readiness snapshot (status /
 *     status_note / status_changed_at) + name/kind/identifier;
 *   - INNER JOIN jobs (status = 'active') for project_name — dropping any equipment whose latest
 *     location has a NULL / closed / on_hold job (that equipment is no longer "on a job").
 * `jobs.status` is the canonical active-job filter: the lifecycle write route keeps `status` in
 * lock-step with `lifecycle` (status = lifecycle==='active' ? 'active' : 'closed'), and the Job
 * Tracker filters on `j.status`.
 *
 * Read-only; FULLY bound (the single `'active'` literal is bound as ?1 for trust-boundary hygiene
 * even though the route takes NO client input); leaks nothing beyond the projected fields. NOT
 * capped/paginated ON PURPOSE — the daemon needs the COMPLETE snapshot to compute retire-off-job
 * correctly (a truncated page would wrongly retire equipment beyond the cap); the equipment fleet
 * is naturally bounded (a 10–50-person firm's finite inventory), so an uncapped internal read of
 * our own reference table is fine.
 *
 * ALSO returns `jobs_with_equipment` — the RECONCILE ROSTER: every ACTIVE job that has ANY
 * `equipment_location` row ever (regardless of current on-job count). This is load-bearing: a job
 * whose CURRENT equipment complement has dropped to ZERO produces NO `equipment` rows, so without
 * the roster the daemon would never revisit that job's Equipment sheet and its stale
 * `On Job=Active` rows would persist forever. The daemon iterates the roster as its reconcile set;
 * a job in the roster but absent from `equipment` gets ALL its sheet rows retired (Off Job).
 */
app.get("/api/internal/fieldops/equipment-snapshot", requireFieldopsToken, async (c) => {
  const [snapRes, rosterRes] = await c.env.DB.batch<Record<string, unknown>>([
    c.env.DB
      .prepare(
        `SELECT el.equipment_id, el.job_id, j.project_name,
                e.name, e.kind, e.identifier,
                e.status, e.status_note, e.status_changed_at,
                el.label AS location_label, el.lat, el.lon, el.read_at, el.recorded_at
           FROM (
             SELECT id, equipment_id, job_id, label, lat, lon, read_at, recorded_at,
                    ROW_NUMBER() OVER (PARTITION BY equipment_id
                                       ORDER BY recorded_at DESC, id DESC) AS rn
               FROM equipment_location
           ) el
           JOIN equipment e ON e.id = el.equipment_id AND e.active = 1
           JOIN jobs j ON j.job_id = el.job_id AND j.status = ?1
          WHERE el.rn = 1
          ORDER BY j.project_name ASC, e.name ASC, el.equipment_id ASC`,
      )
      .bind("active"),
    c.env.DB
      .prepare(
        `SELECT DISTINCT el.job_id, j.project_name
           FROM equipment_location el
           JOIN jobs j ON j.job_id = el.job_id AND j.status = ?1
          WHERE el.job_id IS NOT NULL
          ORDER BY j.project_name ASC, el.job_id ASC`,
      )
      .bind("active"),
  ]);
  return c.json({
    equipment: snapRes.results ?? [],
    jobs_with_equipment: rosterRes.results ?? [],
  });
});

/**
 * GET /material-list-snapshot — the per-job Material List across all active jobs (P7 Material List
 * up-sync, M2). A SNAPSHOT, not an event drain: it re-projects the operator-authored per-job
 * expected-materials list every cycle, so — like the equipment snapshot and unlike the job / hours
 * queues — there is NO watermark and NO mark-mirrored companion. One object per ACTIVE
 * (`jem.active = 1`) `job_expected_materials` row on an ACTIVE job:
 *   - INNER JOIN jobs (status = 'active') for project_name — dropping any line whose job is
 *     closed/on_hold/unknown (the same active-job filter the equipment snapshot uses;
 *     `jobs.status` is kept in lock-step with `lifecycle` by the lifecycle write route);
 *   - LEFT JOIN material_catalog (as a correlated subquery, matching the expected-materials READ
 *     route) → `catalog_name` = the catalog model_id for a catalog-picked line, NULL for free-text;
 *   - `received_by_display` resolves the stored ACCOUNT username → the personnel DISPLAY NAME only
 *     (House Reflex §5 / W9 — an unmatched account yields NULL, the raw username never leaves the
 *     Worker), resolved EXACTLY as the expected-materials read route resolves received_by;
 *   - `part_number` / `category` / `expected_ship_date` (0059). 0059 deliberately left this route on
 *     pre-0059 columns and deferred their mirror exposure to PR4 — this is that exposure.
 *     `expected_ship_date` is the SHIP date; `expected_date` keeps its 0031 meaning of expected
 *     DELIVERY, which is why both are projected rather than one replacing the other.
 *
 * Read-only; FULLY bound (the single `'active'` literal is bound as ?1 for trust-boundary hygiene
 * even though the route takes NO client input); leaks nothing beyond the projected fields. NOT
 * capped/paginated ON PURPOSE — the daemon needs the COMPLETE list per job to compute
 * retire-removed correctly (a truncated page would wrongly mark still-active lines Removed); a
 * per-job expected-materials list is naturally bounded, so an uncapped internal read of our own
 * reference table is fine (mirrors the equipment-snapshot rationale).
 *
 * ALSO returns `jobs_with_materials` — the RECONCILE ROSTER: every ACTIVE job that has ANY
 * `job_expected_materials` row (active OR deactivated). This is load-bearing: a job whose lines were
 * ALL deactivated produces NO `lines` rows, so without the roster the daemon would never revisit
 * that job's Material List sheet and its stale `On List=Active` rows would persist forever. The
 * daemon iterates the roster as its reconcile set; a job in the roster but absent from `lines` gets
 * ALL its sheet rows marked Removed. (No `jem.active` filter here — that is deliberate.)
 */
app.get("/api/internal/fieldops/material-list-snapshot", requireFieldopsToken, async (c) => {
  const [linesRes, rosterRes] = await c.env.DB.batch<Record<string, unknown>>([
    c.env.DB
      .prepare(
        `SELECT jem.line_uuid, jem.job_id, j.project_name, jem.material_id,
                (SELECT mc.model_id FROM material_catalog mc WHERE mc.id = jem.material_id) AS catalog_name,
                jem.description, jem.qty, jem.unit, jem.expected_date, jem.status,
                jem.part_number, jem.category, jem.expected_ship_date,
                jem.received_at, jem.qty_received,
                (SELECT p.name FROM personnel p WHERE p.username = jem.received_by ORDER BY p.id ASC LIMIT 1)
                  AS received_by_display,
                jem.note, jem.unplanned, jem.seq
           FROM job_expected_materials jem
           JOIN jobs j ON j.job_id = jem.job_id AND j.status = ?1
          WHERE jem.active = 1
          ORDER BY j.project_name ASC, jem.seq ASC, jem.id ASC`,
      )
      .bind("active"),
    c.env.DB
      .prepare(
        `SELECT DISTINCT jem.job_id, j.project_name
           FROM job_expected_materials jem
           JOIN jobs j ON j.job_id = jem.job_id AND j.status = ?1
          ORDER BY j.project_name ASC, jem.job_id ASC`,
      )
      .bind("active"),
  ]);
  return c.json({
    lines: linesRes.results ?? [],
    jobs_with_materials: rosterRes.results ?? [],
  });
});

/**
 * P7 Material Incidents up-sync (M3 Slice 2) — the field-ops Material Incidents ledger route
 * (GET /api/internal/fieldops/material-incidents). Same field-ops token privilege separation as the
 * job/hours/equipment/material-list mirror queues (requireFieldopsToken — NOT portal_poll's internal
 * token nor the admin token).
 *
 * An APPEND-ONLY EVENT LEDGER, NOT a re-projected snapshot: each row is a FILED (box_verified=1),
 * §34-screened `material-incident%` submission — an immutable field event. Unlike the material-list
 * snapshot there is deliberately NO reconcile roster and the daemon NEVER retires a row (a reported
 * incident happened; it is never "removed"), so the count-drops-to-zero / #468 zero-drop class is
 * structurally impossible here — there is no retire path to wrongly zero. The active-job JOIN bounds
 * the working set (incidents on non-active jobs drop out and their sheet is archive-moved on closure);
 * within that bound the set grows monotonically, covered downstream by the Smartsheet §51 row-cap
 * watchdog. Uncapped like the sibling (a per-job incident count is small); if TOTAL active-job
 * incident volume ever grows materially, add a `LIMIT` + a `since`/created_at cursor here — a FUTURE
 * optimization, do NOT build it now (the change-only daemon upsert already no-ops on re-projection).
 *
 * Only box_verified=1 is returned: a still-unfiled (0) submission is mid-pipeline and a rejected
 * (-1, e.g. a malicious photo) one must NEVER surface. The incident's structured fields live inside
 * `payload_json` (json_extract) — line_uuid is a submission VALUE (M3 Slice 1), not a column.
 * `line_status` LEFT-JOINs the referenced expected-materials line (unique line_uuid ⇒ ≤1 match) so a
 * later receipt flipping the line to 'received' shows as the live resolution signal; an unlinked or
 * since-deleted line → NULL. DISPLAY-NAME-ONLY reported_by (personnel.name; the raw actor_username is
 * never returned — House Reflex §5). All values flow only to a Smartsheet cell (no AI, no send).
 */
app.get("/api/internal/fieldops/material-incidents", requireFieldopsToken, async (c) => {
  const { results } = await c.env.DB
    .prepare(
      `SELECT s.submission_uuid, s.job_id, j.project_name, s.work_date, s.created_at, s.box_link,
              json_extract(s.payload_json, '$.material_description') AS material_description,
              json_extract(s.payload_json, '$.delivery_ref')         AS delivery_ref,
              json_extract(s.payload_json, '$.qty_expected')         AS qty_expected,
              json_extract(s.payload_json, '$.qty_received')         AS qty_received,
              json_extract(s.payload_json, '$.issue')                AS issue,
              json_extract(s.payload_json, '$.details')              AS details,
              json_extract(s.payload_json, '$.action_taken')         AS action_taken,
              json_extract(s.payload_json, '$.line_uuid')            AS line_uuid,
              (SELECT p.name FROM personnel p WHERE p.username = s.actor_username ORDER BY p.id ASC LIMIT 1)
                AS reported_by_display,
              jem.status AS line_status
         FROM submissions s
         JOIN jobs j ON j.job_id = s.job_id AND j.status = ?1
         LEFT JOIN job_expected_materials jem
              ON jem.job_id = s.job_id
             AND jem.line_uuid = json_extract(s.payload_json, '$.line_uuid')
        WHERE s.form_code LIKE 'material-incident%' AND s.box_verified = 1
        ORDER BY j.project_name ASC, s.created_at ASC, s.submission_uuid ASC`,
    )
    .bind("active")
    .all();
  return c.json({ incidents: results ?? [] });
});

/**
 * Material Receipts up-sync (PR4) — the field-ops delivery-ledger route
 * (GET /api/internal/fieldops/material-receipts). Same field-ops token privilege separation as the
 * job/hours/equipment/material-list/material-incidents mirror queues (requireFieldopsToken — NOT
 * portal_poll's internal token nor the admin token).
 *
 * An APPEND-ONLY EVENT LEDGER, NOT a re-projected snapshot — the `material_incidents` posture, not
 * `material_list`'s. Each row is one `material_receipt_events` mark (migration 0059): somebody
 * asserted on a date that a delivery arrived, partly arrived, or did not. That is an immutable
 * historical fact, so there is deliberately NO reconcile roster and the daemon NEVER retires a row,
 * making the count-drops-to-zero / #468 zero-drop class structurally impossible here — there is no
 * retire path to wrongly zero. The active-job JOIN bounds the working set (receipts on non-active
 * jobs drop out and their sheet is archive-moved on closure); within that bound the set grows
 * monotonically, covered downstream by the Smartsheet §51 row-cap watchdog.
 *
 * Unlike the incidents route this reads a real TABLE rather than json_extract over submissions —
 * receipts are structured D1 rows, not form payloads. Two DERIVED columns ride along because the
 * mirror shows them and they legitimately change after the event is written: `line_status` (the
 * line's coarse status, which goes 'incident' if a problem is later flagged) and
 * `line_qty_received` (the ledger ROLLUP across all events for that line, recomputed here rather
 * than stored, so it can never drift from the events it summarizes).
 *
 * `bol_number` is LEFT-JOINed from the optional shipment the event names — a mark made against a
 * specific truckload carries its BOL, one made against the line as a whole does not.
 * DISPLAY-NAME-ONLY received_by (personnel.name; the raw actor username is never returned — House
 * Reflex §5). All values flow only to a Smartsheet cell (no AI, no send).
 */
app.get("/api/internal/fieldops/material-receipts", requireFieldopsToken, async (c) => {
  const { results } = await c.env.DB
    .prepare(
      `SELECT e.event_uuid, e.job_id, j.project_name, e.kind, e.qty, e.note, e.event_date,
              jem.line_uuid, jem.description AS material_description, jem.unit,
              jem.part_number, jem.qty AS line_qty_expected, jem.status AS line_status,
              (SELECT SUM(e2.qty) FROM material_receipt_events e2 WHERE e2.line_id = e.line_id)
                AS line_qty_received,
              sh.bol_number,
              (SELECT p.name FROM personnel p WHERE p.username = e.actor ORDER BY p.id ASC LIMIT 1)
                AS received_by_display
         FROM material_receipt_events e
         JOIN jobs j ON j.job_id = e.job_id AND j.status = ?1
         LEFT JOIN job_expected_materials jem ON jem.id = e.line_id
         LEFT JOIN material_shipments sh ON sh.id = e.shipment_id
        ORDER BY j.project_name ASC, e.id ASC`,
    )
    .bind("active")
    .all();
  return c.json({ receipts: results ?? [] });
});

/**
 * Operator user provisioning — /api/internal/admin/* (requireAdminToken, the
 * operator-only secret). The operator passes PLAINTEXT over this bearer-gated
 * channel; the BACKEND bcrypt-hashes (cost 10) before write — plaintext is never
 * stored, returned, or logged. NOT a self-service UI and NO user-role model; these
 * are operator-run endpoints driven by the Mac `portal_admin` CLI (brief §4).
 */
async function setUserDisabled(
  c: Context<{ Bindings: Env; Variables: Vars }>,
  value: 0 | 1,
): Promise<Response> {
  let body: { username?: unknown };
  try {
    body = await c.req.json();
  } catch {
    return c.json({ error: "bad_request" }, 400);
  }
  // JSON `null`/arrays/scalars PARSE fine but aren't objects; dereferencing body.x on
  // them threw → bare 500 (audit #1). Require a plain object (the `as unknown` cast
  // dodges the no-overlap check on the typed body var).
  if (typeof body !== "object" || (body as unknown) === null || Array.isArray(body)) {
    return c.json({ error: "bad_request" }, 400);
  }
  const username = normalizeUsername(typeof body.username === "string" ? body.username : "");
  if (!username) return c.json({ error: "invalid_username" }, 400);
  // W4 atomicity: the mutation and its audit row land in ONE batch, the audit
  // conditional on changes()=1 — so a 404 (no such user) never writes a lying audit
  // row, and a successful privilege change can never be missing one.
  const [res] = await c.env.DB.batch([
    c.env.DB.prepare("UPDATE users SET disabled=? WHERE username=?").bind(value, username),
    auditStmtIfChanged(c, "operator-cli", value === 1 ? "operator_user_disable" : "operator_user_enable", username, {
      disabled: value,
    }),
  ]);
  if ((res.meta?.changes ?? 0) === 0) return c.json({ error: "not_found" }, 404);
  return c.json({ ok: true, username, disabled: value });
}

/** Validate an optional `role` body field. undefined → `dflt`; 'admin'/'submitter'
 *  → that value; anything else → null (caller returns 400 invalid_role). Never
 *  coerces a junk value to a privilege — an unknown role is rejected, not defaulted. */
// POST /api/internal/admin/users — provision a new user (409 if it exists). Accepts
// an optional `role` (default 'submitter') so the operator can bootstrap the two
// admins via `portal_admin add-user --role admin`.
app.post("/api/internal/admin/users", requireAdminToken, async (c) => {
  let body: { username?: unknown; password?: unknown; role?: unknown };
  try {
    body = await c.req.json();
  } catch {
    return c.json({ error: "bad_request" }, 400);
  }
  // JSON `null`/arrays/scalars PARSE fine but aren't objects; dereferencing body.x on
  // them threw → bare 500 (audit #1). Require a plain object (the `as unknown` cast
  // dodges the no-overlap check on the typed body var).
  if (typeof body !== "object" || (body as unknown) === null || Array.isArray(body)) {
    return c.json({ error: "bad_request" }, 400);
  }
  const username = normalizeUsername(typeof body.username === "string" ? body.username : "");
  const password = typeof body.password === "string" ? body.password : "";
  const role = parseRole(body.role);
  if (!username) return c.json({ error: "invalid_username" }, 400);
  if (password.length < 8 || password.length > 256) return c.json({ error: "invalid_password" }, 400);
  if (role === null) return c.json({ error: "invalid_role" }, 400);
  const exists = await c.env.DB.prepare("SELECT 1 FROM users WHERE username=?").bind(username).first();
  if (exists) return c.json({ error: "exists" }, 409);
  const password_hash = await hashPassword(password); // plaintext never stored/logged
  try {
    // W4 atomicity — creating an account (at ANY role, including admin) is the
    // highest-privilege mutation on this bearer, so its audit row must be
    // inseparable from it. changes()=1 holds for a successful INSERT.
    await c.env.DB.batch([
      c.env.DB
        .prepare("INSERT INTO users (username, password_hash, role) VALUES (?,?,?)")
        .bind(username, password_hash, role),
      auditStmtIfChanged(c, "operator-cli", "operator_user_create", username, { role }),
    ]);
  } catch (e) {
    // Race backstop (audit #5): concurrent create → UNIQUE violation → 409, not 500.
    if (isUniqueViolation(e)) return c.json({ error: "exists" }, 409);
    throw e;
  }
  return c.json({ ok: true, username, role }, 201);
});

// POST /api/internal/admin/users/role — set an existing user's role (404 if absent).
// Operator break-glass for the role model (e.g. restore an admin the UI demoted).
// NO last-admin guard here on purpose: the CLI is the recovery path *out* of a
// zero-admin lockout, so it must never refuse on admin-count grounds.
app.post("/api/internal/admin/users/role", requireAdminToken, async (c) => {
  let body: { username?: unknown; role?: unknown };
  try {
    body = await c.req.json();
  } catch {
    return c.json({ error: "bad_request" }, 400);
  }
  // JSON `null`/arrays/scalars PARSE fine but aren't objects; dereferencing body.x on
  // them threw → bare 500 (audit #1). Require a plain object (the `as unknown` cast
  // dodges the no-overlap check on the typed body var).
  if (typeof body !== "object" || (body as unknown) === null || Array.isArray(body)) {
    return c.json({ error: "bad_request" }, 400);
  }
  const username = normalizeUsername(typeof body.username === "string" ? body.username : "");
  const role = parseRole(body.role, "submitter");
  if (!username) return c.json({ error: "invalid_username" }, 400);
  if (body.role === undefined || role === null) return c.json({ error: "invalid_role" }, 400);
  // W4 atomicity — a role change is a privilege grant/revoke; audit rides the batch.
  const [res] = await c.env.DB.batch([
    c.env.DB.prepare("UPDATE users SET role=? WHERE username=?").bind(role, username),
    auditStmtIfChanged(c, "operator-cli", "operator_user_role_change", username, { role }),
  ]);
  if ((res.meta?.changes ?? 0) === 0) return c.json({ error: "not_found" }, 404);
  return c.json({ ok: true, username, role });
});

// POST /api/internal/admin/users/reset — re-hash an existing user's password (404 if absent).
app.post("/api/internal/admin/users/reset", requireAdminToken, async (c) => {
  let body: { username?: unknown; password?: unknown };
  try {
    body = await c.req.json();
  } catch {
    return c.json({ error: "bad_request" }, 400);
  }
  // JSON `null`/arrays/scalars PARSE fine but aren't objects; dereferencing body.x on
  // them threw → bare 500 (audit #1). Require a plain object (the `as unknown` cast
  // dodges the no-overlap check on the typed body var).
  if (typeof body !== "object" || (body as unknown) === null || Array.isArray(body)) {
    return c.json({ error: "bad_request" }, 400);
  }
  const username = normalizeUsername(typeof body.username === "string" ? body.username : "");
  const password = typeof body.password === "string" ? body.password : "";
  if (!username) return c.json({ error: "invalid_username" }, 400);
  if (password.length < 8 || password.length > 256) return c.json({ error: "invalid_password" }, 400);
  const password_hash = await hashPassword(password); // plaintext never stored/logged
  // Slice 8a (audit #7): a password change BUMPS session_epoch in the SAME UPDATE, so
  // every outstanding cookie for this user is revoked on its next request.
  // W4 atomicity — a password reset also revokes every outstanding session
  // (session_epoch bump), so it must leave a record. NOTE: the detail payload names
  // only the username; the plaintext and the hash never enter the audit row.
  const [res] = await c.env.DB.batch([
    c.env.DB
      .prepare("UPDATE users SET password_hash=?, session_epoch = session_epoch + 1 WHERE username=?")
      .bind(password_hash, username),
    auditStmtIfChanged(c, "operator-cli", "operator_user_password_reset", username, null),
  ]);
  if ((res.meta?.changes ?? 0) === 0) return c.json({ error: "not_found" }, 404);
  return c.json({ ok: true, username });
});

// POST /api/internal/admin/users/disable — disabled=1; /enable — disabled=0.
app.post("/api/internal/admin/users/disable", requireAdminToken, (c) => setUserDisabled(c, 1));
app.post("/api/internal/admin/users/enable", requireAdminToken, (c) => setUserDisabled(c, 0));

// GET /api/internal/admin/users — list users (NO password hashes).
app.get("/api/internal/admin/users", requireAdminToken, async (c) => {
  const { results } = await c.env.DB
    .prepare("SELECT username, role, disabled, created_at FROM users ORDER BY username")
    .all<{ username: string; role: string; disabled: number; created_at: number }>();
  return c.json({ users: results });
});

// POST /api/internal/admin/purge-job — operator hard-delete of a job + ALL its D1 rows
// (submissions, the filed_pdfs PDF cache, and pdf_requests). This is the explicit operator
// path the daemon /api/internal/sync deliberately CANNOT take: sync refuses an empty job set
// (so a transient empty ITS_Active_Jobs read can never wipe the dropdown), which means a
// fully-removed/test job otherwise lingers active=1 forever. D1 is a transport cache — Box +
// the week sheet remain the system of record; this only clears the local copy. One atomic
// batch (cascade children before parents) + an audit_log entry. Idempotent: an unknown job_id
// returns ok:true, found:false with zero counts.
app.post("/api/internal/admin/purge-job", requireAdminToken, async (c) => {
  let body: { job_id?: unknown };
  try {
    body = await c.req.json();
  } catch {
    return c.json({ error: "bad_request" }, 400);
  }
  if (typeof body !== "object" || (body as unknown) === null || Array.isArray(body)) {
    return c.json({ error: "bad_request" }, 400);
  }
  const job_id = typeof body.job_id === "string" ? body.job_id.trim() : "";
  if (!job_id || job_id.length > 64) return c.json({ error: "invalid_job_id" }, 400);
  // Full literal SQL (NO template interpolation) so the bound `?` is the only dynamic input:
  // job_id is always parameterized, never concatenated — and there is no string-built query for
  // CodeQL's injection sink to flag. The cascade deletes children (filed_pdfs, pdf_requests via
  // the submissions subquery; the job-keyed per-job content tables job_daily_requirements +
  // job_weekly_report_inputs + job_expected_materials — Slice 1, R3-F4, mirroring their prune.ts
  // guard-union entries;
  // and the five field-ops job-context tables prune.ts guards a job on — checklist_item_states,
  // checklist_instances, time_entries, task_assignments, inspections, equipment_location)
  // BEFORE the parents (submissions, then jobs). The cascade must cover EXACTLY prune.ts's
  // job-context guard union: prune refuses to delete a job holding any of those rows and
  // names this route as the operator path that clears them, so anything guarded there and
  // missing here is a row that can never be removed by any path — and, worse, one this route
  // silently orphans behind a deleted job while reporting ok:true.
  const results = await c.env.DB.batch([
    c.env.DB
      .prepare("DELETE FROM filed_pdfs WHERE submission_uuid IN (SELECT submission_uuid FROM submissions WHERE job_id = ?)")
      .bind(job_id),
    c.env.DB
      .prepare("DELETE FROM pdf_requests WHERE submission_uuid IN (SELECT submission_uuid FROM submissions WHERE job_id = ?)")
      .bind(job_id),
    c.env.DB.prepare("DELETE FROM job_daily_requirements WHERE job_id = ?").bind(job_id),
    // Weekly Production Report office inputs (0067) — the same job-keyed per-job content class as
    // job_daily_requirements above, and deleted for the same reason: it has no time-based prune
    // (it is the small record of what was reported to a client), so purge-job is its ONLY exit.
    // Orphaned, it would be a client-facing safety-statistics and pending-items record surviving
    // behind a job nobody can see.
    c.env.DB.prepare("DELETE FROM job_weekly_report_inputs WHERE job_id = ?").bind(job_id),
    // Materials tracking children BEFORE their parent line (0059). Both denormalize job_id, and
    // both would otherwise be orphaned behind a deleted job exactly the way the five field-ops
    // tables below once were. The delivery ledger is the record-grade one here: an orphaned
    // receipt event is a signed-for delivery nobody can trace back to a job.
    c.env.DB.prepare("DELETE FROM material_receipt_events WHERE job_id = ?").bind(job_id),
    c.env.DB.prepare("DELETE FROM material_shipments WHERE job_id = ?").bind(job_id),
    c.env.DB.prepare("DELETE FROM job_expected_materials WHERE job_id = ?").bind(job_id),
    // Manifest-import pool (0060). Its three children key on manifest_id, not job_id, so each
    // resolves its parents through the job-keyed subquery and MUST run BEFORE the parent row
    // goes. An orphaned chunk row is worse than untidy — it is the original untrusted document
    // bytes surviving behind a job nobody can see any more.
    c.env.DB
      .prepare("DELETE FROM job_manifest_chunks WHERE manifest_id IN (SELECT id FROM job_manifests WHERE job_id = ?)")
      .bind(job_id),
    c.env.DB
      .prepare("DELETE FROM job_manifest_rows WHERE manifest_id IN (SELECT id FROM job_manifests WHERE job_id = ?)")
      .bind(job_id),
    c.env.DB
      .prepare("DELETE FROM job_manifest_previews WHERE manifest_id IN (SELECT id FROM job_manifests WHERE job_id = ?)")
      .bind(job_id),
    c.env.DB.prepare("DELETE FROM job_manifests WHERE job_id = ?").bind(job_id),
    // Schedule-import pool (0066). Same shape as the manifest pool: three children key on
    // schedule_id and resolve their parents through the job-keyed subquery BEFORE the parent
    // row goes. An orphaned schedule chunk is the same hazard as a manifest one — original
    // untrusted document bytes surviving behind a job nobody can see any more.
    c.env.DB
      .prepare("DELETE FROM job_schedule_chunks WHERE schedule_id IN (SELECT id FROM job_schedules WHERE job_id = ?)")
      .bind(job_id),
    c.env.DB
      .prepare("DELETE FROM job_schedule_rows WHERE schedule_id IN (SELECT id FROM job_schedules WHERE job_id = ?)")
      .bind(job_id),
    c.env.DB
      .prepare("DELETE FROM job_schedule_previews WHERE schedule_id IN (SELECT id FROM job_schedules WHERE job_id = ?)")
      .bind(job_id),
    c.env.DB.prepare("DELETE FROM job_schedules WHERE job_id = ?").bind(job_id),
    // Living schedule task list (0071, ADR-0006 PR-4). Job-keyed directly (no subquery
    // needed) and deleted with its pool: an orphaned task row is a live-looking task list
    // behind a job nobody can see. Mirrors its prune.ts jobs-guard entry (in-step rule).
    c.env.DB.prepare("DELETE FROM job_schedule_tasks WHERE job_id = ?").bind(job_id),
    // Job payments (0073, ADR-0006 PR-7). Children first: receipts key on cycle_id, so
    // they resolve their parents through the job-keyed subquery BEFORE the cycles go;
    // terms last. An orphaned receipt is record-grade — a money-received event nobody
    // can trace back to a job — and the terms/cycles rows are commercially sensitive
    // (admin-only in life, so they must not linger behind a deleted job either).
    // Mirrors the prune.ts jobs-guard entries for terms + cycles (in-step rule).
    c.env.DB
      .prepare("DELETE FROM job_payment_receipts WHERE cycle_id IN (SELECT id FROM job_payment_cycles WHERE job_id = ?)")
      .bind(job_id),
    c.env.DB.prepare("DELETE FROM job_payment_cycles WHERE job_id = ?").bind(job_id),
    c.env.DB.prepare("DELETE FROM job_payment_terms WHERE job_id = ?").bind(job_id),
    // The field-ops job-context tables prune.ts already guards a job on. Its guard
    // comment named purge-job as "the explicit operator cleanup path (cascades both)"
    // — it did not: these five were never deleted, so purging a job returned ok:true
    // and a tidy count while payroll/billing-grade time_entries, task_assignments and
    // inspections, plus the checklist and equipment-location trails, were orphaned
    // invisibly behind a now-absent job. Children before parents:
    // checklist_item_states → checklist_instances, and time_entries (whose task_id
    // references task_assignments) → task_assignments.
    c.env.DB
      .prepare("DELETE FROM checklist_item_states WHERE instance_id IN (SELECT id FROM checklist_instances WHERE job_id = ?)")
      .bind(job_id),
    c.env.DB.prepare("DELETE FROM checklist_instances WHERE job_id = ?").bind(job_id),
    c.env.DB.prepare("DELETE FROM time_entries WHERE job_id = ?").bind(job_id),
    c.env.DB.prepare("DELETE FROM task_assignments WHERE job_id = ?").bind(job_id),
    c.env.DB.prepare("DELETE FROM inspections WHERE job_id = ?").bind(job_id),
    c.env.DB.prepare("DELETE FROM equipment_location WHERE job_id = ?").bind(job_id),
    c.env.DB.prepare("DELETE FROM submissions WHERE job_id = ?").bind(job_id),
    c.env.DB.prepare("DELETE FROM jobs WHERE job_id = ?").bind(job_id),
    c.env.DB
      .prepare("INSERT INTO audit_log (actor_username, action, target_username, detail) VALUES (?,?,?,?)")
      .bind("operator-cli", "purge-job", job_id, "hard-delete job + D1 cache"),
  ]);
  // ⚠ POSITIONAL — every index below is the statement's ORDER in the batch above. Inserting a
  // DELETE shifts every index after it, and a mis-shifted index silently mis-reports a count
  // (the operator's only visibility into what this hard-delete removed). Re-check the whole list
  // whenever the batch changes; test/purge-job.test.ts asserts each counter BY NAME so a shift
  // fails loudly instead of quietly attributing one table's rows to another.
  const pdfChunks = results[0]?.meta?.changes ?? 0;
  const pdfRequests = results[1]?.meta?.changes ?? 0;
  const requirements = results[2]?.meta?.changes ?? 0;
  const weeklyReportInputs = results[3]?.meta?.changes ?? 0;
  const receiptEvents = results[4]?.meta?.changes ?? 0;
  const shipments = results[5]?.meta?.changes ?? 0;
  const expectedMaterials = results[6]?.meta?.changes ?? 0;
  const manifestChunks = results[7]?.meta?.changes ?? 0;
  const manifestRows = results[8]?.meta?.changes ?? 0;
  const manifestPreviews = results[9]?.meta?.changes ?? 0;
  const manifests = results[10]?.meta?.changes ?? 0;
  const scheduleChunks = results[11]?.meta?.changes ?? 0;
  const scheduleRows = results[12]?.meta?.changes ?? 0;
  const schedulePreviews = results[13]?.meta?.changes ?? 0;
  const schedules = results[14]?.meta?.changes ?? 0;
  const scheduleTasks = results[15]?.meta?.changes ?? 0;
  const paymentReceipts = results[16]?.meta?.changes ?? 0;
  const paymentCycles = results[17]?.meta?.changes ?? 0;
  const paymentTerms = results[18]?.meta?.changes ?? 0;
  const checklistItemStates = results[19]?.meta?.changes ?? 0;
  const checklistInstances = results[20]?.meta?.changes ?? 0;
  const timeEntries = results[21]?.meta?.changes ?? 0;
  const taskAssignments = results[22]?.meta?.changes ?? 0;
  const inspections = results[23]?.meta?.changes ?? 0;
  const equipmentLocation = results[24]?.meta?.changes ?? 0;
  const submissions = results[25]?.meta?.changes ?? 0;
  const job = results[26]?.meta?.changes ?? 0;
  return c.json({
    ok: true, found: job > 0, job_id, job_deleted: job, submissions, pdfChunks, pdfRequests,
    requirements, expectedMaterials,
    // Weekly Production Report office inputs (0067) — reported separately so the operator sees
    // that the client-facing safety statistics and pending-items record went with the job.
    weeklyReportInputs,
    // Reported per-table so the operator SEES the payroll/billing-grade rows this
    // removed — a silent count is how the old omission stayed invisible.
    receiptEvents, shipments,
    // Manifest pool (0060) — chunks reported separately because that counter is the
    // operator's only confirmation that the untrusted document BYTES went with the job.
    manifests, manifestChunks, manifestRows, manifestPreviews,
    // Schedule pool (0066) — same rule: the chunks counter is the confirmation the
    // untrusted schedule bytes went with the job. scheduleTasks (0071) is the LIVING
    // task list going with it.
    schedules, scheduleChunks, scheduleRows, schedulePreviews, scheduleTasks,
    // Job payments (0073, ADR-0006 PR-7) — reported per-table: paymentReceipts is the
    // record-grade counter (money-received events leaving with the job), and a visible
    // count is how the old five-table omission would have been caught.
    paymentReceipts, paymentCycles, paymentTerms,
    checklistItemStates, checklistInstances, timeEntries, taskAssignments, inspections,
    equipmentLocation,
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// In-app admin surface — /api/admin/* (requireSession + requireRole("admin")).
//
// This is the SESSION+ROLE-gated counterpart to the bearer /api/internal/admin/*
// operator-CLI routes above. A logged-in admin (the CEO / head PM) manages accounts
// from the browser; every route is re-gated server-side (the SPA hiding tabs is NOT
// the boundary — Invariant 2). Each mutation + its audit_log row run in ONE atomic
// D1 batch, so an account change can never land without its security-log entry.
// Nothing here transmits anything externally (Invariant 1) — D1 writes only.
// ─────────────────────────────────────────────────────────────────────────────

interface TargetRow { username: string; role: string; disabled: number }

/**
 * SQL guard fragment for the last-admin protection (operator's call, Q2 = ON).
 *
 * Appended to the demote/delete WHERE so the "is this the only ENABLED admin?" test
 * is evaluated ATOMICALLY inside the mutation: the count subquery sees the row's
 * pre-mutation state at write time. This is deliberately NOT a separate pre-SELECT —
 * a check-then-act pair is a TOCTOU race (two concurrent demotes/deletes could both
 * read count=2, both pass, and strand zero admins). With the guard inline, each
 * UPDATE/DELETE re-evaluates the count and at most one matches a row; the loser
 * matches 0 rows (meta.changes==0 ⇒ the caller returns 409 last_admin).
 *
 * Only an ENABLED admin target is guarded — a disabled admin isn't a functioning
 * admin to protect (matches the count's `disabled=0`). The bearer break-glass routes
 * are deliberately NOT guarded (they are the recovery path out of a zero-admin state).
 */
function lastAdminGuardClause(target: TargetRow): string {
  return target.role === "admin" && !target.disabled
    ? " AND (SELECT COUNT(*) FROM users WHERE role='admin' AND disabled=0) > 1"
    : "";
}

const adminGate = [requireSession, requireRole("admin")] as const;

// GET /api/admin/users — list all accounts (username, role, disabled, created_at). No hashes.
app.get("/api/admin/users", ...adminGate, async (c) => {
  const { results } = await c.env.DB
    .prepare("SELECT username, role, disabled, created_at FROM users ORDER BY username")
    .all<{ username: string; role: string; disabled: number; created_at: number }>();
  return c.json({ users: results });
});

// POST /api/admin/users — create an account (role selectable; 409 if it exists).
app.post("/api/admin/users", ...adminGate, async (c) => {
  let body: { username?: unknown; password?: unknown; role?: unknown };
  try {
    body = await c.req.json();
  } catch {
    return c.json({ error: "bad_request" }, 400);
  }
  // JSON `null`/arrays/scalars PARSE fine but aren't objects; dereferencing body.x on
  // them threw → bare 500 (audit #1). Require a plain object (the `as unknown` cast
  // dodges the no-overlap check on the typed body var).
  if (typeof body !== "object" || (body as unknown) === null || Array.isArray(body)) {
    return c.json({ error: "bad_request" }, 400);
  }
  const username = normalizeUsername(typeof body.username === "string" ? body.username : "");
  const password = typeof body.password === "string" ? body.password : "";
  const role = parseRole(body.role);
  if (!username) return c.json({ error: "invalid_username" }, 400);
  if (password.length < 8 || password.length > 256) return c.json({ error: "invalid_password" }, 400);
  if (role === null) return c.json({ error: "invalid_role" }, 400);
  const exists = await c.env.DB.prepare("SELECT 1 FROM users WHERE username=?").bind(username).first();
  if (exists) return c.json({ error: "exists" }, 409);
  const password_hash = await hashPassword(password); // plaintext never stored/logged
  try {
    await c.env.DB.batch([
      c.env.DB.prepare("INSERT INTO users (username, password_hash, role) VALUES (?,?,?)")
        .bind(username, password_hash, role),
      auditStmt(c, c.get("session").username, "user_create", username, { role }),
    ]);
  } catch (e) {
    // Lost the check-then-act race (a concurrent create of the same username) → the
    // UNIQUE constraint fires here. Map to 409, not a bubbled 500 (audit #5). The
    // `if (exists)` pre-check above is the cheap path; this is the race backstop.
    if (isUniqueViolation(e)) return c.json({ error: "exists" }, 409);
    throw e;
  }
  return c.json({ ok: true, username, role }, 201);
});

// POST /api/admin/users/credentials — edit a login: new_username and/or new_password
// (own or any other account). Editing YOUR OWN login re-issues the session (the
// cookie is cleared → the SPA forces a re-login with the new credentials).
app.post("/api/admin/users/credentials", ...adminGate, async (c) => {
  let body: { username?: unknown; new_username?: unknown; new_password?: unknown };
  try {
    body = await c.req.json();
  } catch {
    return c.json({ error: "bad_request" }, 400);
  }
  // JSON `null`/arrays/scalars PARSE fine but aren't objects; dereferencing body.x on
  // them threw → bare 500 (audit #1). Require a plain object (the `as unknown` cast
  // dodges the no-overlap check on the typed body var).
  if (typeof body !== "object" || (body as unknown) === null || Array.isArray(body)) {
    return c.json({ error: "bad_request" }, 400);
  }
  const username = normalizeUsername(typeof body.username === "string" ? body.username : "");
  if (!username) return c.json({ error: "invalid_username" }, 400);
  const hasNewUsername = body.new_username !== undefined;
  const hasNewPassword = body.new_password !== undefined;
  if (!hasNewUsername && !hasNewPassword) return c.json({ error: "no_changes" }, 400);

  const target = await c.env.DB
    .prepare("SELECT username, role, disabled FROM users WHERE username=?")
    .bind(username)
    .first<TargetRow>();
  if (!target) return c.json({ error: "not_found" }, 404);

  const sets: string[] = [];
  const binds: unknown[] = [];
  let renamedTo: string | null = null;

  if (hasNewUsername) {
    const nu = normalizeUsername(typeof body.new_username === "string" ? body.new_username : "");
    if (!nu) return c.json({ error: "invalid_new_username" }, 400);
    if (nu !== target.username) {
      const taken = await c.env.DB.prepare("SELECT 1 FROM users WHERE username=?").bind(nu).first();
      if (taken) return c.json({ error: "username_taken" }, 409);
      sets.push("username=?");
      binds.push(nu);
      renamedTo = nu;
    }
  }
  if (hasNewPassword) {
    const np = typeof body.new_password === "string" ? body.new_password : "";
    if (np.length < 8 || np.length > 256) return c.json({ error: "invalid_password" }, 400);
    sets.push("password_hash=?");
    binds.push(await hashPassword(np)); // plaintext never stored/logged
    // Slice 8a (audit #7): a password change BUMPS session_epoch, revoking every
    // outstanding cookie for the target on its next request. No bind param (literal
    // SET), so the binds-order ↔ sets-order alignment for the placeholders is intact.
    sets.push("session_epoch = session_epoch + 1");
  }
  if (sets.length === 0) return c.json({ error: "no_changes" }, 400); // new_username == current, no password

  try {
    await c.env.DB.batch([
      c.env.DB.prepare(`UPDATE users SET ${sets.join(", ")} WHERE username=?`).bind(...binds, target.username),
      auditStmt(c, c.get("session").username, "user_edit", target.username, {
        username_changed: renamedTo !== null,
        renamed_to: renamedTo,
        password_changed: hasNewPassword,
      }),
    ]);
  } catch (e) {
    // A concurrent rename into the same target username loses the UNIQUE race → 409
    // (audit #5; the `taken` pre-check above is the cheap path, this is the backstop).
    if (isUniqueViolation(e)) return c.json({ error: "username_taken" }, 409);
    throw e;
  }

  // Self-edit → re-auth. A username change already invalidates the cookie (the
  // per-request lookup is by the OLD username); a password change does not, so we
  // clear it explicitly. Either way the SPA lands on the login screen.
  if (target.username === c.get("session").username) {
    deleteCookie(c, COOKIE, { path: "/" });
    return c.json({ ok: true, reauth: true });
  }
  return c.json({ ok: true, username: renamedTo ?? target.username });
});

// POST /api/admin/users/role — change an account's role (submitter ⇄ admin).
// Last-admin guard: cannot demote the only enabled admin (Q2). Self-demote re-auths.
app.post("/api/admin/users/role", ...adminGate, async (c) => {
  let body: { username?: unknown; role?: unknown };
  try {
    body = await c.req.json();
  } catch {
    return c.json({ error: "bad_request" }, 400);
  }
  // JSON `null`/arrays/scalars PARSE fine but aren't objects; dereferencing body.x on
  // them threw → bare 500 (audit #1). Require a plain object (the `as unknown` cast
  // dodges the no-overlap check on the typed body var).
  if (typeof body !== "object" || (body as unknown) === null || Array.isArray(body)) {
    return c.json({ error: "bad_request" }, 400);
  }
  const username = normalizeUsername(typeof body.username === "string" ? body.username : "");
  const role = parseRole(body.role, "submitter");
  if (!username) return c.json({ error: "invalid_username" }, 400);
  if (body.role === undefined || role === null) return c.json({ error: "invalid_role" }, 400);

  const target = await c.env.DB
    .prepare("SELECT username, role, disabled FROM users WHERE username=?")
    .bind(username)
    .first<TargetRow>();
  if (!target) return c.json({ error: "not_found" }, 404);
  if (target.role === role) return c.json({ ok: true, username, role, changed: false });

  // Atomic demote: the last-admin guard lives in the UPDATE's WHERE (see
  // lastAdminGuardClause) so it can't race a concurrent demote. The audit row is
  // inserted ONLY when the UPDATE matched — `changes()` reflects the prior statement
  // within the batch's single transaction, so mutation+audit stay atomic and no audit
  // is written for a guard-blocked attempt.
  const res = await c.env.DB.batch([
    c.env.DB.prepare(`UPDATE users SET role=? WHERE username=?${lastAdminGuardClause(target)}`)
      .bind(role, target.username),
    auditStmtIfChanged(c, c.get("session").username, "role_change", target.username, { from: target.role, to: role }),
  ]);
  // changes==0 is overloaded: the atomic last-admin guard blocked it, OR a concurrent
  // delete removed the row after our load. Re-check existence so the code is honest
  // (audit #6): 404 if gone, 409 last_admin if genuinely still the last enabled admin.
  if ((res[0]?.meta?.changes ?? 0) === 0) {
    const still = await c.env.DB.prepare("SELECT 1 FROM users WHERE username=?").bind(target.username).first();
    return still ? c.json({ error: "last_admin" }, 409) : c.json({ error: "not_found" }, 404);
  }

  if (target.username === c.get("session").username) {
    deleteCookie(c, COOKIE, { path: "/" });
    return c.json({ ok: true, reauth: true });
  }
  return c.json({ ok: true, username, role, changed: true });
});

// POST /api/admin/users/delete — delete an account. Last-admin guard applies.
// Self-delete is permitted (unless it strands no admin) and re-auths the caller.
app.post("/api/admin/users/delete", ...adminGate, async (c) => {
  let body: { username?: unknown };
  try {
    body = await c.req.json();
  } catch {
    return c.json({ error: "bad_request" }, 400);
  }
  // JSON `null`/arrays/scalars PARSE fine but aren't objects; dereferencing body.x on
  // them threw → bare 500 (audit #1). Require a plain object (the `as unknown` cast
  // dodges the no-overlap check on the typed body var).
  if (typeof body !== "object" || (body as unknown) === null || Array.isArray(body)) {
    return c.json({ error: "bad_request" }, 400);
  }
  const username = normalizeUsername(typeof body.username === "string" ? body.username : "");
  if (!username) return c.json({ error: "invalid_username" }, 400);

  const target = await c.env.DB
    .prepare("SELECT username, role, disabled FROM users WHERE username=?")
    .bind(username)
    .first<TargetRow>();
  if (!target) return c.json({ error: "not_found" }, 404);

  // Atomic delete: same in-WHERE last-admin guard + changes()-conditional audit as
  // the role route — the count subquery sees the pre-delete state, so concurrent
  // deletes/demotes can't both strand the last enabled admin.
  const res = await c.env.DB.batch([
    c.env.DB.prepare(`DELETE FROM users WHERE username=?${lastAdminGuardClause(target)}`)
      .bind(target.username),
    auditStmtIfChanged(c, c.get("session").username, "user_delete", target.username, { role: target.role }),
  ]);
  // Same overloaded changes==0 as the role route (audit #6): guard-blocked (still the
  // last enabled admin) vs already-deleted by a concurrent request. 404 if gone.
  if ((res[0]?.meta?.changes ?? 0) === 0) {
    const still = await c.env.DB.prepare("SELECT 1 FROM users WHERE username=?").bind(target.username).first();
    return still ? c.json({ error: "last_admin" }, 409) : c.json({ error: "not_found" }, 404);
  }

  if (target.username === c.get("session").username) {
    deleteCookie(c, COOKIE, { path: "/" });
    return c.json({ ok: true, reauth: true });
  }
  return c.json({ ok: true, username: target.username });
});

// ── Form editor publish pipeline (Phase 2, slice 3a) ───────────────────────────
// SEND-FREE: POST /api/admin/publish VALIDATES the composed definition server-side
// (publishValidation, design C3) and, only if valid, ENQUEUES a publish_requests row.
// It NEVER commits or deploys — the Mac daemon (slice 3b) is the sole privileged
// actuator (mirrors the External Send Gate: the cloud can only queue). create / edit /
// add_version carry a composed definition; delete / rollback flip the manifest at
// actuation and carry only the target.
const PUBLISH_OPS = new Set(["create", "edit", "add_version", "delete", "rollback", "recategorize"]);
// A publish still in flight, for per-parent serialization (C8) — archived | failed are
// terminal. 'live' still blocks (the Box-archive stage is pending). A crashed publish no
// longer wedges a parent forever: the Worker's LEASE_TTL_S makes a stale lease re-claimable
// (pending/claim), and the Mac daemon's stale-row sweep (publish_daemon._sweep_stale_rows)
// stamps any non-terminal row stalled past STALE_RECLAIM_S to failed('stale_reclaimed') — both
// added in PR-2 to MAKE THIS TRUE (it previously described a daemon watchdog that did not exist).
const NON_TERMINAL_STATUSES = "('queued','validated','tested','merged','live')";

app.post("/api/admin/publish", ...adminGate, async (c) => {
  let body: {
    op?: unknown; identity?: unknown; parent_form_code?: unknown;
    target_form_code?: unknown; definition?: unknown; category?: unknown;
  };
  try {
    body = await c.req.json();
  } catch {
    return c.json({ error: "bad_request" }, 400);
  }
  if (typeof body !== "object" || (body as unknown) === null || Array.isArray(body)) {
    return c.json({ error: "bad_request" }, 400);
  }
  const op = typeof body.op === "string" ? body.op : "";
  if (!PUBLISH_OPS.has(op)) return c.json({ error: "invalid_op" }, 400);
  const identity = typeof body.identity === "string" ? body.identity : "";
  const parent = typeof body.parent_form_code === "string" ? body.parent_form_code : "";
  if (!/^[a-z0-9-]+$/.test(identity)) return c.json({ error: "invalid_identity" }, 400);
  if (!/^[a-z0-9-]+$/.test(parent)) return c.json({ error: "invalid_parent_form_code" }, 400);

  // Workflow category — REQUIRED for recategorize; OPTIONAL for create (absent → apply_publish
  // defaults the new parent to safety; the SPA selector always sends it). Validated against the
  // workflows.json registry (mirrors apply_publish's re-check). The other ops ignore it → null.
  let category: string | null = null;
  if (op === "recategorize" || (op === "create" && body.category !== undefined)) {
    const cat = validateCategory(body.category);
    if (!cat.ok) return c.json({ error: "invalid_category", reason: cat.reason }, 400);
    category = body.category as string;
  }

  // create/edit/add_version carry a composed definition → server-side validate it (C3).
  // delete/rollback carry only the target (the daemon flips the manifest at actuation).
  let definitionJson: string | null = null;
  let targetFormCode: string | null =
    typeof body.target_form_code === "string" ? body.target_form_code : null;
  if (op === "create" || op === "edit" || op === "add_version") {
    const result = validateDefinition(body.definition, { identity, parentFormCode: parent });
    if (!result.ok) return c.json({ error: "invalid_definition", reason: result.reason }, 400);
    // Catalog-level parent-grouping guard: create/add_version add a NEW form to the parent,
    // which must not mix a standalone form with variants (edit bumps an existing identity →
    // grouping unchanged). Mirrors apply_publish; the daemon re-checks vs live git HEAD.
    if (op !== "edit") {
      const grouping = validateParentGrouping(
        catalog, parent, (body.definition as { variant_label?: string | null }).variant_label,
      );
      if (!grouping.ok) return c.json({ error: "invalid_definition", reason: grouping.reason }, 400);
    }
    targetFormCode = (body.definition as { form_code: string }).form_code;
    definitionJson = JSON.stringify(body.definition);
  } else if (targetFormCode !== null && !/^[a-z0-9-]+-v[0-9]+$/.test(targetFormCode)) {
    return c.json({ error: "invalid_target_form_code" }, 400);
  }

  // Per-parent serialization (C8): reject a 2nd publish while one is in flight.
  const inflight = await c.env.DB
    .prepare(`SELECT id FROM publish_requests WHERE parent_form_code=? AND status IN ${NON_TERMINAL_STATUSES} LIMIT 1`)
    .bind(parent)
    .first();
  if (inflight) return c.json({ error: "publish_in_progress" }, 409);

  const res = await c.env.DB.batch([
    c.env.DB.prepare(
      "INSERT INTO publish_requests (requested_by, op, parent_form_code, identity, target_form_code, definition_json, category) VALUES (?,?,?,?,?,?,?)",
    ).bind(c.get("session").username, op, parent, identity, targetFormCode, definitionJson, category),
    auditStmt(c, c.get("session").username, "form_publish", identity, {
      op, target_form_code: targetFormCode, ...(category !== null ? { category } : {}),
    }),
  ]);
  return c.json({ ok: true, id: res[0]?.meta?.last_row_id ?? null, status: "queued" }, 201);
});

// GET /api/admin/publish-status — the status-monitor read view (most-recent first).
// Send-free read of the publish_requests state machine for the admin dashboard stepper.
app.get("/api/admin/publish-status", ...adminGate, async (c) => {
  const { results } = await c.env.DB
    .prepare(
      "SELECT id, created_at, updated_at, requested_by, op, parent_form_code, identity, " +
        "target_form_code, status, failed_stage, failure_reason FROM publish_requests ORDER BY id DESC LIMIT 50",
    )
    .all();
  return c.json({ requests: results });
});

// POST /api/admin/publish-dismiss — clear TERMINAL (archived | failed) requests from the
// monitor. Send-free; only finished rows are removed — an in-flight publish is never
// touched (the form files + audit_log remain the record). Returns the count cleared.
app.post("/api/admin/publish-dismiss", ...adminGate, async (c) => {
  const res = await c.env.DB
    .prepare("DELETE FROM publish_requests WHERE status IN ('archived', 'failed')")
    .run();
  return c.json({ ok: true, cleared: res.meta?.changes ?? 0 });
});

// GET /api/admin/publish-request?id=N — fetch ONE request's full record INCLUDING the
// composed definition_json, so a FAILED publish can be re-opened in the editor and fixed
// instead of losing the work. Send-free read.
app.get("/api/admin/publish-request", ...adminGate, async (c) => {
  const id = Number(c.req.query("id"));
  if (!Number.isInteger(id) || id <= 0) return c.json({ error: "invalid_id" }, 400);
  const row = await c.env.DB
    .prepare(
      "SELECT id, op, parent_form_code, identity, target_form_code, status, definition_json, category " +
        "FROM publish_requests WHERE id = ?",
    )
    .bind(id)
    .first();
  if (!row) return c.json({ error: "not_found" }, 404);
  return c.json({ request: row });
});

// ── Publish daemon interface (Phase 2, slice 3b) ───────────────────────────────
// The Mac publish daemon's bearer-gated queue interface: pull queued requests, ATOMICALLY
// LEASE one (so two daemon runs can't actuate the same row), and STAMP the state machine
// as it commits / deploys. The daemon is the sole privileged actuator; the Worker only
// exposes the queue (send-free). Same PORTAL_INTERNAL_API_TOKEN as the portal_poll daemon.
const PUBLISH_STATUSES = new Set(["queued", "validated", "tested", "merged", "live", "archived", "failed"]);

// Lease TTL (PR-2): a claimed-but-stalled row (the daemon died after claim, before any stamp)
// becomes re-claimable once its lease is older than this. Must exceed the daemon's CI wait +
// deploy slack so a legitimately in-progress publish is never stolen. 30 min.
const LEASE_TTL_S = 30 * 60;

// Legal predecessors per stamp target (PR-2): the stamp endpoint only advances a row whose
// CURRENT status is a legal predecessor of the requested status. Blocks a forged / out-of-order
// stamp on the shared internal token (an archived→queued revert, a queued→archived skip) and a
// re-stamp of a terminal row. 'queued' is absent (the initial state is never a stamp target);
// 'live' accepts 'tested' (the daemon folds the merge into its tested stage) OR 'merged';
// 'failed' accepts any non-terminal state.
const LEGAL_PREDECESSORS: Record<string, string[]> = {
  validated: ["queued"],
  tested: ["validated"],
  merged: ["tested"],
  live: ["tested", "merged"],
  archived: ["live"],
  failed: ["queued", "validated", "tested", "merged", "live"],
};

// GET /api/internal/publish/pending — claimable rows (queued + unleased OR stale-leased), oldest-first.
app.get("/api/internal/publish/pending", requireInternalToken, async (c) => {
  const limit = Math.min(Number(c.req.query("limit")) || 20, 100);
  const { results } = await c.env.DB
    .prepare(
      "SELECT id, created_at, requested_by, op, parent_form_code, identity, target_form_code, definition_json " +
        "FROM publish_requests WHERE status='queued' AND (lease_owner IS NULL OR lease_at < unixepoch() - ?) " +
        "ORDER BY id ASC LIMIT ?",
    )
    .bind(LEASE_TTL_S, limit)
    .all();
  return c.json({ pending: results });
});

// POST /api/internal/publish/claim — ATOMICALLY lease a queued row for one daemon run.
// { id, lease_owner } leases ONLY if still queued AND (unleased OR its lease is stale past
// LEASE_TTL_S — takeover of a dead daemon's lease). Two LIVE runs still can't both actuate.
// Returns the full row (incl. definition_json) when claimed.
app.post("/api/internal/publish/claim", requireInternalToken, async (c) => {
  let body: Record<string, unknown>;
  try { body = await c.req.json(); } catch { return c.json({ error: "bad_request" }, 400); }
  if (typeof body !== "object" || (body as unknown) === null || Array.isArray(body)) {
    return c.json({ error: "bad_request" }, 400);
  }
  const id = typeof body.id === "number" && Number.isInteger(body.id) ? body.id : 0;
  const lease_owner = typeof body.lease_owner === "string" ? body.lease_owner.slice(0, 128) : "";
  if (!id || !lease_owner) return c.json({ error: "invalid" }, 400);
  const res = await c.env.DB
    .prepare("UPDATE publish_requests SET lease_owner=?, lease_at=unixepoch() WHERE id=? AND status='queued' AND (lease_owner IS NULL OR lease_at < unixepoch() - ?)")
    .bind(lease_owner, id, LEASE_TTL_S)
    .run();
  if ((res.meta?.changes ?? 0) === 0) return c.json({ ok: true, claimed: false });
  const request = await c.env.DB
    .prepare("SELECT id, op, parent_form_code, identity, target_form_code, definition_json, category, status FROM publish_requests WHERE id=?")
    .bind(id)
    .first();
  return c.json({ ok: true, claimed: true, request });
});

// POST /api/internal/publish/stamp — advance the state machine. { id, status,
// failed_stage?, failure_reason? }. The daemon stamps validated→tested→merged→live→
// archived, or failed (with stage + reason) on any error. failed_stage/reason are kept
// ONLY for a failed stamp (cleared otherwise).
app.post("/api/internal/publish/stamp", requireInternalToken, async (c) => {
  let body: Record<string, unknown>;
  try { body = await c.req.json(); } catch { return c.json({ error: "bad_request" }, 400); }
  if (typeof body !== "object" || (body as unknown) === null || Array.isArray(body)) {
    return c.json({ error: "bad_request" }, 400);
  }
  const id = typeof body.id === "number" && Number.isInteger(body.id) ? body.id : 0;
  const status = typeof body.status === "string" ? body.status : "";
  if (!id || !PUBLISH_STATUSES.has(status)) return c.json({ error: "invalid" }, 400);
  const failed = status === "failed";
  const failed_stage = failed && typeof body.failed_stage === "string" ? body.failed_stage.slice(0, 64) : null;
  const failure_reason = failed && typeof body.failure_reason === "string" ? body.failure_reason.slice(0, 2000) : null;
  // State-machine guard (PR-2): only advance a row whose CURRENT status is a legal predecessor
  // of the requested status — blocks a forged / out-of-order stamp on the shared internal token.
  const preds = LEGAL_PREDECESSORS[status];
  if (!preds) return c.json({ error: "invalid" }, 400); // 'queued' is never a stamp target
  const placeholders = preds.map(() => "?").join(",");
  const res = await c.env.DB
    .prepare(
      "UPDATE publish_requests SET status=?, failed_stage=?, failure_reason=?, updated_at=unixepoch() " +
        `WHERE id=? AND status IN (${placeholders})`,
    )
    .bind(status, failed_stage, failure_reason, id, ...preds)
    .run();
  if ((res.meta?.changes ?? 0) === 0) {
    // changes==0 is overloaded: the row is gone, OR its current status isn't a legal predecessor
    // of `status` (a forged / out-of-order stamp). Re-read for an honest reason; the row was NOT
    // advanced either way. 200 + found:false keeps the daemon's stamp contract (it never makes an
    // illegal transition, so it never sees this; a forger is simply rejected).
    const row = await c.env.DB.prepare("SELECT status FROM publish_requests WHERE id=?").bind(id).first<{ status: string }>();
    if (!row) return c.json({ ok: true, found: false });
    return c.json({ ok: true, found: false, reason: `illegal transition ${row.status} -> ${status}` });
  }
  return c.json({ ok: true, found: true });
});

// GET /api/internal/publish/stuck?older_than=<sec> — non-terminal rows whose updated_at is older
// than the cutoff (a publish that crashed mid-actuation, or a stalled stage). The Mac daemon's
// stale-row sweep (publish_daemon._sweep_stale_rows) reclaims these by stamping
// failed('stale_reclaimed') so they stop wedging the parent's C8 in-flight check. Bearer-gated.
app.get("/api/internal/publish/stuck", requireInternalToken, async (c) => {
  const olderThan = Math.min(Math.max(Number(c.req.query("older_than")) || 0, 0), 86400);
  const { results } = await c.env.DB
    .prepare(
      "SELECT id, status, lease_owner, lease_at, updated_at, op, parent_form_code, identity " +
        `FROM publish_requests WHERE status IN ${NON_TERMINAL_STATUSES} AND updated_at < unixepoch() - ? ` +
        "ORDER BY id ASC LIMIT 50",
    )
    .bind(olderThan)
    .all();
  return c.json({ stuck: results });
});

// Unmatched /api/* → JSON 404 (never the SPA shell).
app.all("/api/*", (c) => c.json({ error: "not_found" }, 404));

// Everything else → the built SPA via the static-assets binding. With
// run_worker_first:["/api/*"] most non-API requests are served as assets before
// the Worker runs; this fallback covers the SPA shell where the Worker does run.
app.get("*", (c) => c.env.ASSETS.fetch(c.req.raw));

// ── scheduled (A3): the daily cron (wrangler.jsonc triggers.crons) prunes the D1 store.
// SEND-FREE like every other path (Invariant 1) — it only deletes aged local rows. A prune
// failure is logged via observability and does not affect the fetch path.
// GS2: pruneOldData never throws for a per-stage failure (each stage is fenced; failures
// accumulate in failedStages), and the prune_meta heartbeat row is written after EVERY run —
// success or fail — so the Mac watchdog (Check V, via GET /api/internal/prune-status) pages
// on failed stages / staleness / the 6 GB size condition instead of a console.log nobody tails.
const scheduled: ExportedHandlerScheduledHandler<Env> = async (_controller, env) => {
  const nowSec = Math.floor(Date.now() / 1000);
  const pruned = await pruneOldData(env.DB, nowSec);
  console.log(
    `prune: stripped ${pruned.stripped} payload(s), removed ${pruned.submissions} inactive-job + ` +
      `${pruned.rejected} rejected submission(s) + ${pruned.audit} audit row(s) + ` +
      `${pruned.pdfRequests} pdf request(s) + ${pruned.pdfChunks} pdf chunk(s) + ` +
      `${pruned.publishRequests} terminal publish request(s) + ` +
      `${pruned.itemPhotos} stuck/orphaned item photo(s) + ` +
      `${pruned.dailyPhotos} abandoned/orphaned daily pool photo(s) + ${pruned.jobs} empty job(s); ` +
      `D1 size ${pruned.dbSizeBytes} bytes` +
      (pruned.failedStages.length > 0 ? `; FAILED stages: ${pruned.failedStages.join(", ")}` : ""),
  );
  await writePruneMeta(env.DB, nowSec, pruned); // fenced inside — never takes down the handler

  // Recurring checklists (#16) — spawn today's due per-job instances. Ships DARK: no-op unless the
  // RECURRING_CHECKLISTS_ENABLED var is "true". Fenced separately from prune (a generation error must
  // never take down the prune leg, and vice-versa). Send-free (D1 only, Invariant 1).
  if (env.RECURRING_CHECKLISTS_ENABLED === "true") {
    try {
      const gen = await generateRecurringChecklists(env.DB, Date.now());
      console.log(
        `recurring-checklists: ${gen.recurrences} active def(s), ${gen.instances_created} instance(s) created, ` +
          `${gen.autostopped} auto-stopped (job closed), ${gen.capped} catch-up-capped, ${gen.errors} error(s)`,
      );
    } catch (e) {
      // Whole-pass fence (per-recurrence errors are already fenced inside) — never-silent, never takes
      // down the cron.
      console.error(`recurring-checklists generation pass failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  }
};

export default { fetch: app.fetch, scheduled } satisfies ExportedHandler<Env>;
