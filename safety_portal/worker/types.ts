// Cloudflare bindings + shared types for the Safety Portal Worker.
// Bindings are declared in wrangler.jsonc; @cloudflare/workers-types supplies
// the D1Database / Fetcher globals.
//
// No R2: under Box-as-System-of-Record + Option-B render the Worker never holds a
// PDF (intake.py renders + stores it in Box). Removed 2026-06-05.

export interface Env {
  /** D1 database. Phase 2: `users` table. Later phases: submissions + mirrors. */
  DB: D1Database;
  /** Static-asset fetcher (the built SPA). Bound via assets.binding in wrangler.jsonc. */
  ASSETS: Fetcher;
  /** HMAC key for signing session cookies. Workers Secret / .dev.vars — never committed. */
  SESSION_SIGNING_SECRET: string;
  /**
   * HMAC-SHA256 key the Worker signs each submission with at /api/submit (Phase 5
   * pull model). The Mac-side portal_poll daemon verifies it (mirrored into the
   * macOS Keychain as ITS_PORTAL_HMAC_SECRET). Workers Secret / .dev.vars — never committed.
   */
  HMAC_PAYLOAD_SECRET: string;
  /**
   * Bearer token the Mac-side portal_poll daemon presents to /api/internal/* (the
   * queue drain + the receipt). Mirrored into the Keychain as ITS_PORTAL_INTERNAL_TOKEN.
   * Workers Secret / .dev.vars — never committed.
   */
  PORTAL_INTERNAL_API_TOKEN: string;
  /**
   * OPERATOR-ONLY bearer token for the /api/internal/admin/* user-provisioning
   * routes — SEPARATE from PORTAL_INTERNAL_API_TOKEN so the portal_poll daemon's
   * token cannot create / reset / disable users (privilege separation). Mirrored
   * into the Keychain as ITS_PORTAL_ADMIN_TOKEN. Workers Secret / .dev.vars —
   * never committed.
   */
  PORTAL_ADMIN_API_TOKEN: string;
  /**
   * Bearer token the Mac-side field-ops mirror daemon (field_ops/fieldops_sync.py)
   * presents to /api/internal/fieldops/* — SEPARATE from PORTAL_INTERNAL_API_TOKEN and
   * PORTAL_ADMIN_API_TOKEN so the mirror daemon's token cannot drain the submission queue
   * or provision users (privilege separation). Mirrored into the Keychain as
   * ITS_PORTAL_FIELDOPS_TOKEN. The endpoints + bearer guard land in P2; this binding is
   * declared in P0 so wrangler.jsonc / .dev.vars can carry it. Workers Secret — never committed.
   */
  PORTAL_FIELDOPS_API_TOKEN: string;
  /**
   * Bearer token the Mac-side PO daemon (po_materials/po_poll.py, WS1 S4) presents to
   * /api/po/internal/* — SEPARATE from PORTAL_INTERNAL_API_TOKEN, PORTAL_ADMIN_API_TOKEN and
   * PORTAL_FIELDOPS_API_TOKEN so the PO daemon's token cannot drain the submission queue,
   * provision users, or touch the job/hours mirror (privilege separation), and none of those
   * tokens can read the PO queue. Mirrored into the Keychain as ITS_PORTAL_PO_TOKEN.
   * Workers Secret / .dev.vars — never committed.
   */
  PORTAL_PO_API_TOKEN: string;
  /**
   * Bearer token the Mac-side estimate daemon (po_materials/estimate_poll.py, ADR-0004 E2)
   * presents to /api/po/estimates/internal/* — SEPARATE from PORTAL_PO_API_TOKEN and every
   * other tier (ADR-0004 decision 4 / red-team #1): the highest-exposure process (it decodes
   * hostile PDF/xlsx bytes) holds a token scoped ONLY to the estimate pool; it must NOT reach
   * the PO queue, the mirrors, user provisioning, or any send-lane control surface — and none
   * of those tokens may read the estimate pool. Mirrored into the Keychain as
   * ITS_PORTAL_ESTIMATE_TOKEN. Workers Secret / .dev.vars — never committed.
   */
  PORTAL_ESTIMATE_API_TOKEN: string;
  /**
   * Bearer token the Mac-side RFQ daemons (po_materials/rfq_poll.py + rfq_send_poll.py,
   * ADR-0004 R2/R3) present to /api/po/rfqs/internal/* — SEPARATE from BOTH
   * PORTAL_PO_API_TOKEN and PORTAL_ESTIMATE_API_TOKEN and every other tier (ADR-0004
   * decision 4 / red-team #1): a compromised extraction daemon (the highest-exposure
   * process) must NOT reach the RFQ send-lane control surface, so the RFQ tier mints its
   * OWN token scoped ONLY to the RFQ queue — and none of the sibling tokens may
   * read/advance it. Mirrored into the Keychain as ITS_PORTAL_RFQ_TOKEN. Workers Secret /
   * .dev.vars — never committed.
   */
  PORTAL_RFQ_API_TOKEN: string;
  /**
   * Bearer token the Mac-side manifest daemon (field_ops/manifest_poll.py, PR3b) presents to
   * /api/fieldops/manifests/internal/* — SEPARATE from every other tier, for exactly the
   * ADR-0004 decision-4 reason the estimate lane has its own: this daemon decodes hostile
   * PDF/xlsx bytes (openpyxl + pdfplumber inside a killable child), so it is a
   * highest-exposure process and its token is scoped ONLY to the manifest pool. It must NOT
   * reach the PO/RFQ/estimate queues, the submission drain, user provisioning, the mirrors,
   * or any send-lane control surface — and none of those tokens may read the manifest pool.
   * Mirrored into the Keychain as ITS_PORTAL_MANIFEST_TOKEN. Workers Secret / .dev.vars —
   * never committed.
   */
  PORTAL_MANIFEST_API_TOKEN: string;
  /**
   * Bearer token the Mac-side config daemon (config_editor/config_poll.py, §50 — built LATER)
   * presents to /api/internal/config/* — SEPARATE from the portal_poll / admin / fieldops / PO
   * tokens (privilege separation): the config daemon's token must NOT be able to drain the
   * submission queue, provision users, touch the job/hours mirror, or read the PO queue — and
   * none of those tokens may read/advance the config-edit queue. Mirrored into the Keychain as
   * ITS_PORTAL_CONFIG_TOKEN. Workers Secret / .dev.vars — never committed.
   */
  PORTAL_CONFIG_API_TOKEN: string;
  /**
   * Bearer token the Mac-side subcontract daemon (subcontracts/subcontract_poll.py, SC-S3c/S4)
   * presents to /api/subcontracts/internal/* — SEPARATE from the portal_poll / admin / fieldops /
   * PO / config tokens (privilege separation): the subcontract daemon's token must NOT drain the
   * submission queue, provision users, touch the job/hours mirror, or read the PO / config queues —
   * and none of those tokens may read the subcontract queue. Mirrored into the Keychain as
   * ITS_PORTAL_SUB_TOKEN. Workers Secret / .dev.vars — never committed.
   */
  PORTAL_SUB_API_TOKEN: string;
  /**
   * Feature flag (a plain Worker `var`, NOT a secret) gating "recurring checklists per job" (#16).
   * "true" arms the scheduled() cron's cadence-generation pass AND lets POST /checklist/assign accept
   * a recurrence block; anything else (incl. absent) keeps the feature DARK — the cron no-ops and the
   * assign route refuses a recurrence block with 400 recurring_disabled (never-silent). Declared in
   * wrangler.jsonc `vars` (default "false"); flip to "true" + `npm run deploy` to activate. Exposed to
   * the SPA via /api/login + /api/session so the assign form only shows the recurring controls live.
   */
  RECURRING_CHECKLISTS_ENABLED: string;
  /**
   * Feature flag (a plain Worker `var`, NOT a secret) gating "checklist/inspection completion →
   * weekly progress report" (#17, Seam A). "true" arms POST
   * /api/fieldops/checklist/instance/:id/submit: a completed inspection's assignee signs off and the
   * Worker synthesizes a category:'progress' `checklist-completion-v1` submission that rides the
   * EXISTING intake → progress-week-sheet → weekly-compile pipeline (a standard submission, NOT a new
   * §51 SoR write-route). Anything else (incl. absent) keeps the feature DARK — the emit route
   * refuses with 400 progress_logging_disabled (never-silent) and the SPA hides the "Sign & log"
   * action. Declared in wrangler.jsonc `vars` (default "false"); flip to "true" + `npm run deploy` to
   * activate (routing to the progress week-sheet + progress@ mailbox ALSO needs the separate
   * ITS_Config progress_reports.intake_enabled flip). Exposed to the SPA via /api/login +
   * /api/session as checklist_progress_logging_enabled.
   */
  CHECKLIST_PROGRESS_LOGGING_ENABLED: string;
}

/** A portal user's authorization role. 'submitter' is the default for every field
 *  PM; 'manager' (P2.6) is the mid-tier crew lead (personnel + crew-assign, NO job/task
 *  create, NO login minting); 'admin' unlocks the dashboard (account management + submit-as).
 *  The granted capability SET — not this role string — is what routes gate on; role is used
 *  only for the few admin-ONLY hard-checks (idle-timeout, admin surface, submit-as, login
 *  minting), which correctly exclude 'manager'. */
export type Role = "submitter" | "manager" | "admin";

/** Claims signed (NOT encrypted) into the session cookie. Keep minimal — readable by
 *  the holder. Deliberately NO role here: role is read fresh from D1 per request
 *  (see requireSession), so a demotion takes effect immediately rather than waiting
 *  for the 90-day cookie to expire — same reasoning as the per-request `disabled`
 *  check. A stale signed role in the cookie would be a privilege-escalation footgun. */
export interface SessionClaims {
  /** users.id of the authenticated portal user. */
  sub: number;
  /** username (lastname.firstname) — display only. */
  username: string;
  /** issued-at, unix seconds. */
  iat: number;
  /** Revocation epoch (slice 8a, audit #7). Snapshot of users.session_epoch at issue
   *  time; requireSession rejects when this is BEHIND the live DB epoch. UNLIKE `role`
   *  this MUST live in the cookie — it is the captured-cookie kill switch (logout /
   *  password-change bump the DB epoch, leaving the old cookie's snapshot stale). A
   *  pre-#7 cookie has NO epoch claim → requireSession treats it as 0 (DEFAULT 0), so
   *  existing sessions survive this migration. */
  epoch?: number;
}

/** Hono per-request variables. */
export interface Vars {
  session: SessionClaims;
  /** The acting user's role, read fresh from D1 by requireSession on every request
   *  (NOT from the cookie). requireRole() and the submit-as gate read this. */
  role: Role;
  /** The acting user's capability SET, resolved fresh from D1 (migration 0013's
   *  role_capabilities) by requireSession every request — same per-request,
   *  change-effective-next-request posture as `role`. requireCapability() reads this.
   *  FAIL-CLOSED: empty set on unknown role / D1 error (see auth.resolveCapabilities). */
  capabilities: Set<string>;
}
