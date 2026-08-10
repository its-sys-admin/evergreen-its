import type { Context } from "hono";
import type { FieldopsApp, FieldopsGates } from "./fieldops_gates";
import type { Env, Vars } from "./types";
import type { AdditionalPhotoRef, DailyPhotoUploadResult, DailyPoolPhotoRow } from "./wire-types";
import { auditStmtIfChanged } from "./audit";
import { hmacHex } from "./hmac";
import { b64DecodedLen, isPhotoItem, validateSinglePhoto } from "./photo_bounds";
import { requireJob, requireJobScope } from "./fieldops_scope";

// ─────────────────────────────────────────────────────────────────────────────────────────────────
// Daily-report photo POOL (DR-photo-pool Slice 1, operator directive 2026-07-03: "add more photo
// holding sections … as many of those as you need in the daily field report").
//
// WHY: the inline `site_photos` field is payload-budgeted (CS2: 280KB × 4 ≈ 1.49MB base64 <
// PAYLOAD_MAX 1.8MB) — more inline photos structurally CANNOT ride the submission payload. Each
// ADDITIONAL photo therefore uploads INDIVIDUALLY here (its own request, the G1 item-photo bounds
// verbatim), lands in the daily_photo_pool pending queue (migration 0037), and the SUBMISSION
// carries only tiny REFERENCES (values.additional_photos = [{pool_id, caption?}]) that /api/submit
// validates + CLAIMS via claimAdditionalPhotos below.
//
// OPTION D POSTURE (inherited from the G1 item-photo capture): record-only — NO serving route,
// ever; delete-on-screen (the Slice-2 Mac §34 pass NULLs photo_json on disposition); the SPA
// renders STATUS ONLY (pending / clean / refused — the G1 chip vocabulary).
//
// GATES: session + the closed-vocabulary ROLE check (DAILY_PHOTO_ROLES below — manager/admin, the
// same closed Role vocabulary the daily family gates on; coordinate-by-convention with the
// parallel daily-family role-gate slice) + the per-job placement scope (requireJobScope, the
// /daily-form/status pattern, with the daily family's own bypass-cap set).
// ─────────────────────────────────────────────────────────────────────────────────────────────────

type Ctx = Context<{ Bindings: Env; Variables: Vars }>;

// The daily family's closed-vocabulary role check (worker/types.ts Role is the CLOSED
// "submitter" | "manager" | "admin" — migration-seeded only, coerceRole admits nothing else).
// The daily report is a crew-lead-manager surface; admins keep operator access. NOTE: a parallel
// slice is adding the same manager/admin role gate to the daily-family routes — same convention,
// same vocabulary, deliberately duplicated per-module like the divergent SCOPE_BYPASS_CAPS sets.
const DAILY_PHOTO_ROLES: ReadonlySet<string> = new Set(["manager", "admin"]);

// The daily family's scope-bypass caps (the same set fieldops_daily_requirements.ts passes —
// admins hold both; a manager/submitter holds neither). Kept module-local on purpose: bypass sets
// are intentionally divergent per surface (see fieldops_scope.ts module header).
const SCOPE_BYPASS_CAPS = ["cap.jobtracker.manage", "cap.checklist.manage"] as const;

// SINGLE CONSISTENT BODY BOUND (the A7 413-mismatch class; the exact ITEM_PHOTO_BODY_MAX
// derivation): one photo per request — PHOTO_MAX_BYTES = 400_000 decoded → 533_336 base64 chars,
// + meta caps (name 100 + taken_at 40 + gps 64) + the envelope (job_id ≤ 64 + work_date 10 +
// JSON keys/quotes/braces ≲ 120) → every VALID body fits in ≤ ~533_700 chars; 560_000 leaves
// slack without admitting a second photo's worth of bytes. Checked on the RAW text BEFORE
// JSON.parse (a hostile 10MB body never reaches the parser).
const DAILY_PHOTO_BODY_MAX = 560_000;

/** Per-(job, work_date, uploader) pool cap — non-refused rows (a refusal vacates its slot, the
 *  G1 rule). 40 bounds a photo-heavy day (10× the inline field) while capping D1/Box growth. */
export const POOL_CAP_PER_DAY = 40;

/** Pool-wide PENDING backstop: a dead Slice-2 screening loop must cap D1 growth at
 *  ~200 × 400KB ≈ 80MB, not grow unbounded. NOT the alerting path — a dead portal_poll pages via
 *  watchdog Check C / ITS_Daemon_Health within hours; this is the ceiling while that fires. */
export const POOL_PENDING_GLOBAL_MAX = 200;

/** Max additional-photo REFERENCES a submission may carry — the pool cap (a submission can never
 *  reference more than one uploader-day's pool). Consumed by /api/submit (index.ts). */
export const MAX_ADDITIONAL_PHOTO_REFS = POOL_CAP_PER_DAY;

const MAX_CAPTION = 300; // per-ref caption bound (display text, untrusted downstream)
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

/**
 * DAILY-PHOTO HMAC CANONICAL STRING (mirrored by the Slice-2 Mac verifier, the sibling of
 * shared/portal_hmac.py's item-photo recompute). ORDER + SEPARATOR are load-bearing:
 *
 *   "daily_photo:v1" \n <job_id> \n <work_date> \n <photo_json>
 *
 * joined with "\n"; HMAC-SHA256(HMAC_PAYLOAD_SECRET) → lowercase hex — the SAME key + encoding
 * as the submission/item-photo HMACs so the Mac side reuses its keychain secret + helper.
 *   • The "daily_photo:v1" literal DOMAIN-SEPARATES this protocol from submission HMACs (uuid-
 *     first) and item-photo HMACs ("item_photo:v1") — cross-protocol signature confusion is
 *     structurally impossible — and versions the string for Slice-2 evolution.
 *   • job_id + work_date bind the photo to its day (a valid signed photo cannot be replayed onto
 *     a different job or date without failing verification). The pool row id can't participate —
 *     it doesn't exist until the INSERT the signature rides in.
 *   • photo_json is the EXACT stored JSON string ({data,name,taken_at,gps,uploaded_by} — the
 *     4-key wire PhotoValue + the AUTHENTICATED uploader), used verbatim on both sides, exactly
 *     like the item-photo contract. uploaded_by rides INSIDE photo_json so the Slice-2
 *     malicious-disposition CRITICAL names the account from HMAC-covered data.
 */
function dailyPhotoCanonical(jobId: string, workDate: string, photoJson: string): string {
  return ["daily_photo:v1", jobId, workDate, photoJson].join("\n");
}

// ── /api/submit integration: reference validation + the atomic claim ────────────────────────────

/** Parse + bound-check values.additional_photos. Returns the deduplication-checked refs, null when
 *  the key is absent (no additional photos — the overwhelmingly common submission), an EMPTY array
 *  when the key is present but empty (nothing to claim, but DISTINCT from absent so /api/submit
 *  can still release a prior same-uuid attempt's claims on a drop-all-refs replace — see
 *  releaseAllPhotoClaimsStmt), or a machine reason string for a 400
 *  { error: "invalid_additional_photos", detail }. */
export function parseAdditionalPhotoRefs(values: Record<string, unknown>): AdditionalPhotoRef[] | null | string {
  const raw = values.additional_photos;
  if (raw === undefined) return null;
  if (!Array.isArray(raw)) return "refs_not_array";
  if (raw.length === 0) return []; // present-but-empty: no claims, but stale claims still release
  if (raw.length > MAX_ADDITIONAL_PHOTO_REFS) return "too_many_refs";
  const seen = new Set<number>();
  const refs: AdditionalPhotoRef[] = [];
  for (const r of raw) {
    if (typeof r !== "object" || r === null || Array.isArray(r)) return "ref_not_object";
    const o = r as Record<string, unknown>;
    // Exact-shape: pool_id required, caption optional — nothing else (a foreign key would be
    // silently-filed attacker-controlled payload riding payload_json).
    for (const k of Object.keys(o)) if (k !== "pool_id" && k !== "caption") return "ref_unknown_key";
    if (typeof o.pool_id !== "number" || !Number.isInteger(o.pool_id) || o.pool_id < 1) return "ref_bad_pool_id";
    if (o.caption !== undefined && (typeof o.caption !== "string" || o.caption.length > MAX_CAPTION)) {
      return "ref_bad_caption";
    }
    if (seen.has(o.pool_id)) return "ref_duplicate_pool_id";
    seen.add(o.pool_id);
    refs.push({ pool_id: o.pool_id, ...(o.caption !== undefined ? { caption: o.caption } : {}) });
  }
  return refs;
}

export type ClaimOutcome =
  | { ok: true }
  | { ok: false; status: 409 | 422; error: string };

interface ClaimParams {
  submissionUuid: string;
  jobId: string;
  workDate: string;
  /** The TRUE session actor (submissions.actor_username) — pool rows are claimable by their
   *  uploader only (the daily tab is a self-submit surface; submit-as never borrows photos). */
  actor: string;
  /** The VERIFIED amends target, or null for no-transfer. NEVER the raw body value: /api/submit
   *  resolves body.amends_uuid against the live submissions table (target EXISTS + same
   *  (job_id, work_date) family) BEFORE it reaches this predicate; anything unverifiable arrives
   *  here as null, so a hostile body naming a random / foreign submission's uuid can never
   *  transfer claims through the `claimed_by_submission = amendsUuid` arm below. */
  amendsUuid: string | null;
}

interface PoolCheckRow {
  id: number;
  job_id: string;
  work_date: string;
  uploaded_by: string;
  status: string;
  claimed_by_submission: string | null;
}

/**
 * Validate every referenced pool row and CLAIM it for `submissionUuid` — called by /api/submit
 * BEFORE the submission INSERT (claim-first: a submission row never exists with unclaimed refs,
 * so the Slice-2 filing pass can trust the claim markers unconditionally).
 *
 * A ref is claimable iff the row EXISTS, belongs to (jobId, workDate), was uploaded by `actor`,
 * is not refused, and is unclaimed — OR already claimed by THIS submissionUuid (the designed
 * lost-ACK same-uuid retry), OR claimed by the VERIFIED `amendsUuid` (an amendment re-listing the
 * filed report's photos TRANSFERS the claim to the new uuid; see ClaimParams.amendsUuid — the
 * caller verifies the target against the submissions table, never the raw body string).
 *
 * ATOMICITY: the claims run as guard-in-WHERE UPDATEs in ONE db.batch (a D1 transaction). A lost
 * race (another submission claimed a row between the pre-check and the batch) surfaces as
 * meta.changes === 0 — the batch then gets COMPENSATED (each row this uuid claimed is restored to
 * its pre-check claim value, guarded WHERE claimed_by_submission = this uuid) and the caller
 * returns 409. The double-claim loser therefore leaves zero footprint.
 *
 * The batch also RELEASES rows a prior same-uuid attempt claimed that this payload no longer
 * references (the same-actor replace path) — no stranded claims pointing at a submission whose
 * payload dropped them.
 *
 * 4xx mapping (enumeration-safe): a missing row, a foreign job/date, and a foreign uploader are
 * indistinguishable → 422 unknown_photo_ref (pool ids are guessable integers; existence of other
 * accounts' rows must not leak). An own refused row → 422 photo_refused. An own row claimed by a
 * DIFFERENT submission → 409 photo_already_claimed.
 */
export async function claimAdditionalPhotos(
  c: Ctx,
  refs: AdditionalPhotoRef[],
  p: ClaimParams,
): Promise<ClaimOutcome> {
  const ids = refs.map((r) => r.pool_id);
  const placeholders = ids.map((_, i) => `?${i + 1}`).join(",");
  const res = await c.env.DB.prepare(
    `SELECT id, job_id, work_date, uploaded_by, status, claimed_by_submission
     FROM daily_photo_pool WHERE id IN (${placeholders})`,
  )
    .bind(...ids)
    .all<PoolCheckRow>();
  const byId = new Map((res.results ?? []).map((r) => [r.id, r]));

  const priorClaim = new Map<number, string | null>();
  for (const id of ids) {
    const row = byId.get(id);
    // Missing / wrong job / wrong date / not the actor's upload — one indistinguishable reason.
    if (!row || row.job_id !== p.jobId || row.work_date !== p.workDate || row.uploaded_by !== p.actor) {
      return { ok: false, status: 422, error: "unknown_photo_ref" };
    }
    if (row.status === "refused") return { ok: false, status: 422, error: "photo_refused" };
    const claimed = row.claimed_by_submission;
    if (claimed !== null && claimed !== p.submissionUuid && claimed !== p.amendsUuid) {
      return { ok: false, status: 409, error: "photo_already_claimed" };
    }
    priorClaim.set(id, claimed);
  }

  // The atomic claim batch: one guard-in-WHERE UPDATE per ref + the stale-claim release. The
  // guards repeat the FULL pre-check predicate so a row mutated between check and batch (the
  // lost race) simply doesn't match — changes 0, compensated below.
  const stmts = refs.map((r) =>
    c.env.DB
      .prepare(
        `UPDATE daily_photo_pool SET claimed_by_submission = ?1
         WHERE id = ?2 AND job_id = ?3 AND work_date = ?4 AND uploaded_by = ?5
           AND status IN ('pending','clean')
           AND (claimed_by_submission IS NULL OR claimed_by_submission = ?1
                OR (?6 IS NOT NULL AND claimed_by_submission = ?6))`,
      )
      .bind(p.submissionUuid, r.pool_id, p.jobId, p.workDate, p.actor, p.amendsUuid),
  );
  // Release claims a prior same-uuid attempt holds on rows this payload no longer references
  // (the audited same-actor replace path) — they return to the pre-submit pool.
  stmts.push(
    c.env.DB
      .prepare(
        `UPDATE daily_photo_pool SET claimed_by_submission = NULL
         WHERE claimed_by_submission = ?1 AND id NOT IN (${ids.map((_, i) => `?${i + 2}`).join(",")})`,
      )
      .bind(p.submissionUuid, ...ids),
  );
  const results = await c.env.DB.batch(stmts);

  const lost = refs.filter((_, i) => (results[i].meta.changes ?? 0) === 0 && priorClaim.get(refs[i].pool_id) !== p.submissionUuid);
  const won = refs.filter((_, i) => (results[i].meta.changes ?? 0) === 1);
  if (lost.length > 0) {
    // COMPENSATE: restore each row THIS uuid just claimed to its pre-check claim value (NULL or
    // the amends uuid), guarded on our own claim marker so we can never clobber a third party's.
    const restore = won
      .filter((r) => priorClaim.get(r.pool_id) !== p.submissionUuid)
      .map((r) =>
        c.env.DB
          .prepare(
            "UPDATE daily_photo_pool SET claimed_by_submission = ?1 WHERE id = ?2 AND claimed_by_submission = ?3",
          )
          .bind(priorClaim.get(r.pool_id) ?? null, r.pool_id, p.submissionUuid),
      );
    if (restore.length > 0) await c.env.DB.batch(restore);
    return { ok: false, status: 409, error: "photo_already_claimed" };
  }
  return { ok: true };
}

/** Build (not execute) the DROP-ALL-REFS twin of claimAdditionalPhotos' in-batch stale-claim
 *  release: NULL every claim `submissionUuid` holds. /api/submit batches it WITH the same-uuid
 *  REPLACE INSERT whenever the new payload carries NO additional_photos (key absent OR an empty
 *  list — parseAdditionalPhotoRefs distinguishes both from a malformed list), because the claim
 *  path (and its release) never runs then: without this, a re-submit that dropped every ref would
 *  STRAND its prior claims — rows invisible to the pre-submit pool list, undeletable (409
 *  photo_claimed), and counted against the day's cap forever. Guarded on the claim marker only,
 *  so a fresh uuid (nothing ever claimed) is a clean no-op. */
export function releaseAllPhotoClaimsStmt(c: Ctx, submissionUuid: string): D1PreparedStatement {
  return c.env.DB
    .prepare("UPDATE daily_photo_pool SET claimed_by_submission = NULL WHERE claimed_by_submission = ?1")
    .bind(submissionUuid);
}

// ── the pool routes ──────────────────────────────────────────────────────────────────────────────

export function registerDailyPhotoRoutes(app: FieldopsApp, gates: FieldopsGates): void {
  // ── POST /api/fieldops/daily-photo — upload ONE additional photo into the pool. ────────────────
  // Gates: session → closed-vocabulary role (manager/admin) → body bounds (raw-text-first) →
  // job exists → placement scope → per-day cap + global pending backstop (both ATOMIC — folded
  // into the INSERT's own WHERE, not a pre-read; see the batch below). Bounds are the VERBATIM
  // per-photo /api/submit gate (photo_bounds.validateSinglePhoto). HMAC-signed like item photos
  // (dailyPhotoCanonical above). Queue INSERT + conditional audit in ONE batch (W4). Never log
  // photo bytes.
  app.post("/api/fieldops/daily-photo", gates.requireSession, async (c) => {
    if (!DAILY_PHOTO_ROLES.has(c.get("role"))) return c.json({ error: "forbidden" }, 403);

    // SINGLE CONSISTENT BODY BOUND — on the RAW text BEFORE JSON.parse (see the derivation).
    let raw: string;
    try {
      raw = await c.req.text();
    } catch {
      return c.json({ error: "bad_request" }, 400);
    }
    if (raw.length > DAILY_PHOTO_BODY_MAX) return c.json({ error: "photo_upload_too_large" }, 413);
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch {
      return c.json({ error: "bad_request" }, 400);
    }
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      return c.json({ error: "bad_request" }, 400);
    }
    const body = parsed as Record<string, unknown>;
    const jobId = typeof body.job_id === "string" ? body.job_id : "";
    const workDate = typeof body.work_date === "string" ? body.work_date : "";
    if (!DATE_RE.test(workDate)) return c.json({ error: "invalid_work_date" }, 400);
    const photo = body.photo;
    // Exact wire shape: the 4-key PhotoValue ({data,name,taken_at,gps}, all strings) — the same
    // isPhotoItem the submit gate runs.
    if (!isPhotoItem(photo)) return c.json({ error: "invalid_photo", detail: "invalid_photo_shape" }, 400);
    // The VERBATIM per-photo bounds gate (meta caps → base64 shape → ≤400,000 decoded bytes →
    // JPEG/PNG magic). Machine reason rides `detail` (never the bytes), the /api/submit convention.
    const photoErr = validateSinglePhoto(photo);
    if (photoErr) return c.json({ error: "invalid_photo", detail: photoErr }, 400);

    const jobErr = await requireJob(c, jobId); // 400 bad shape / 404 unknown job
    if (jobErr) return jobErr;
    const scopeErr = await requireJobScope(c, jobId, SCOPE_BYPASS_CAPS); // 403 outside own placement
    if (scopeErr) return scopeErr;

    const actor = c.get("session").username;

    // ── Optional material-line binding (0062) ────────────────────────────────────────────────
    // Both fields are OPTIONAL: an ordinary day photo sends neither and is completely
    // unaffected. When present they are validated HERE, against THIS job's own rows — the
    // binding is established at upload precisely because the submission's photo refs keep their
    // exact {pool_id, caption?} shape, so a foreign key can never ride payload_json in from the
    // client. `parseAdditionalPhotoRefs` stays untouched.
    //
    // 422 rather than 400 on an unknown line/event: the body is well-formed and the caller is
    // authorized — the referenced row simply is not on this job, which is a semantic refusal.
    let lineUuid: string | null = null;
    let receiptEventId: number | null = null;
    if (body.line_uuid !== undefined && body.line_uuid !== null) {
      if (typeof body.line_uuid !== "string" || body.line_uuid.length > 64) {
        return c.json({ error: "invalid_line_uuid" }, 400);
      }
      // Scoped to this job AND active=1: binding to another job's line, or a deactivated one,
      // must fail rather than silently store a cross-job pointer.
      const line = await c.env.DB.prepare(
        "SELECT id FROM job_expected_materials WHERE line_uuid = ?1 AND job_id = ?2 AND active = 1",
      )
        .bind(body.line_uuid, jobId)
        .first<{ id: number }>();
      if (!line) return c.json({ error: "unknown_material_line" }, 422);
      lineUuid = body.line_uuid;

      if (body.receipt_event_id !== undefined && body.receipt_event_id !== null) {
        if (typeof body.receipt_event_id !== "number" || !Number.isInteger(body.receipt_event_id)) {
          return c.json({ error: "invalid_receipt_event_id" }, 400);
        }
        // The event must belong to the line just resolved. Otherwise a photo could be filed
        // against line A while pointing at an event on line B — reading as evidence for a
        // delivery it has nothing to do with.
        const ev = await c.env.DB.prepare(
          "SELECT id FROM material_receipt_events WHERE id = ?1 AND line_id = ?2",
        )
          .bind(body.receipt_event_id, line.id)
          .first<{ id: number }>();
        if (!ev) return c.json({ error: "unknown_receipt_event" }, 422);
        receiptEventId = body.receipt_event_id;
      }
    } else if (body.receipt_event_id !== undefined && body.receipt_event_id !== null) {
      // An event without its line is not bindable: the line is what scopes the check to this
      // job, so accepting the event alone would skip the only ownership test there is.
      return c.json({ error: "receipt_event_without_line" }, 400);
    }

    // Fail closed on a misconfigured Worker: never sign with an undefined secret — a signature
    // the Mac side could never verify would be silent evidence loss (mirrors /api/submit).
    if (!c.env.HMAC_PAYLOAD_SECRET) return c.json({ error: "server_misconfigured" }, 503);
    // photo_json = the EXACT stored string the HMAC covers: the wire PhotoValue + the
    // AUTHENTICATED uploader (the G1 contract — see dailyPhotoCanonical).
    // The 0062 binding rides INSIDE photo_json so the HMAC COVERS it. A bare column would not
    // be signature-covered, so anything trust-bearing (the Mac's screening pass, any later
    // audit) must read the binding out of the verified photo_json — the columns below are a
    // queryable denormalization of these exact values, never the authority.
    //
    // Keys are omitted entirely when unbound rather than written as null, so an ordinary day
    // photo serializes byte-for-byte as it did before 0062 and its signature is unchanged. The
    // Mac parses photo_json with .get() and is key-tolerant, so the added keys are backward-
    // compatible in both directions.
    const photoJson = JSON.stringify({
      data: photo.data,
      name: photo.name,
      taken_at: photo.taken_at,
      gps: photo.gps,
      uploaded_by: actor,
      ...(lineUuid !== null ? { line_uuid: lineUuid } : {}),
      ...(receiptEventId !== null ? { receipt_event_id: receiptEventId } : {}),
    });
    const hmac = await hmacHex(c.env.HMAC_PAYLOAD_SECRET, dailyPhotoCanonical(jobId, workDate, photoJson));

    // (W4) ONE atomic batch: the CAP-FOLDED queue INSERT + the changes()-gated audit. BOTH caps
    // ride the INSERT's own WHERE (INSERT … SELECT … WHERE <cap subqueries> … RETURNING id — the
    // fieldops_time_write head-fold idiom): a SELECT-then-INSERT pre-check is check-then-act, and
    // two concurrent uploads both passing the pre-read would race past the boundary. A cap-refused
    // insert changes ZERO rows, so the conditional audit (auditStmtIfChanged) lands nothing — no
    // lying audit row (W4 discipline). The two subqueries:
    //   • per-(job, date, uploader) cap — non-refused rows (refusals vacate their slot, so a retry
    //     never starves on its own failures); claimed rows COUNT (the cap bounds the day's total);
    //   • pool-wide pending backstop — the D1 growth ceiling while a dead screening loop pages.
    // The audit carries the natural tuple + sizes, NEVER the photo bytes.
    const res = await c.env.DB.batch([
      c.env.DB
        .prepare(
          // The 0062 binding columns are threaded INTO this INSERT…SELECT, never written by a
          // follow-up UPDATE: the caps ride this statement's own WHERE, so a second write would
          // sit outside the atomic fold and could land on a row the caps had refused.
          `INSERT INTO daily_photo_pool (job_id, work_date, uploaded_by, status, photo_json, hmac,
                                         line_uuid, receipt_event_id)
           SELECT ?1, ?2, ?3, 'pending', ?4, ?5, ?6, ?7
           WHERE (SELECT COUNT(*) FROM daily_photo_pool
                  WHERE job_id = ?1 AND work_date = ?2 AND uploaded_by = ?3 AND status != 'refused')
                 < ${POOL_CAP_PER_DAY}
             AND (SELECT COUNT(*) FROM daily_photo_pool WHERE status = 'pending')
                 < ${POOL_PENDING_GLOBAL_MAX}
           RETURNING id`,
        )
        .bind(jobId, workDate, actor, photoJson, hmac, lineUuid, receiptEventId),
      auditStmtIfChanged(c, actor, "daily_photo_add", jobId, {
        job_id: jobId,
        work_date: workDate,
        photo_name: photo.name,
        decoded_bytes: b64DecodedLen(photo.data),
      }),
    ]);
    const poolId = (res[0].results?.[0] as { id: number } | undefined)?.id;
    if (poolId === undefined) {
      // A cap refused the insert (zero rows returned). Disambiguate WHICH by re-reading the two
      // COUNTs post-hoc — best-effort DIAGNOSTICS only (the G2.3 changes()===0 pattern): the
      // counts pick the response, they never gate the write (the fold above already did, atomically).
      const mine = await c.env.DB.prepare(
        "SELECT COUNT(*) AS n FROM daily_photo_pool WHERE job_id = ?1 AND work_date = ?2 AND uploaded_by = ?3 AND status != 'refused'",
      )
        .bind(jobId, workDate, actor)
        .first<{ n: number }>();
      if ((mine?.n ?? 0) >= POOL_CAP_PER_DAY) {
        return c.json({ error: "pool_cap_reached", cap: POOL_CAP_PER_DAY }, 409);
      }
      const backlog = await c.env.DB.prepare(
        "SELECT COUNT(*) AS n FROM daily_photo_pool WHERE status = 'pending'",
      ).first<{ n: number }>();
      if ((backlog?.n ?? 0) >= POOL_PENDING_GLOBAL_MAX) {
        return c.json({ error: "pool_backlogged" }, 503);
      }
      // Neither count exceeds NOW — a raced cap that has since cleared (e.g. a concurrent delete /
      // refusal landed between the refusal and these reads). Same "try again shortly" semantics.
      return c.json({ error: "pool_backlogged" }, 503);
    }
    return c.json({ ok: true, pool_id: poolId, status: "pending" } satisfies DailyPhotoUploadResult, 201);
  });

  // ── GET /api/fieldops/daily-photos?job_id&work_date[&amends] — the actor's OWN pool rows. ──────
  // The SPA's screening-status read (chips pending → clean / refused) + draft-ref reconciliation
  // (a restored draft's ref whose row vanished renders "no longer available"). STATUS ONLY —
  // photo_json is NEVER selected (Option D: no bytes to any browser). Claimed rows are excluded —
  // they belong to a filed submission, not the pre-submit pool — with ONE exception: `amends=`
  // names the filed submission the actor is AMENDING, and rows claimed by THAT uuid are the
  // amended report's own photos (served with claimed=1 so the SPA chips them "Photo on file ✓"
  // instead of lying "missing" — the amend-UX fix). The param is honored only after server-side
  // verification (the target submission EXISTS, actor_username = this session actor, and its
  // (job_id, work_date) match the query — the same never-trust-a-raw-uuid discipline as the
  // /api/submit claim-transfer); anything unverifiable silently degrades to the unclaimed-only
  // read, so a probed foreign uuid is indistinguishable from an unknown one (enumeration-safe).
  app.get("/api/fieldops/daily-photos", gates.requireSession, async (c) => {
    if (!DAILY_PHOTO_ROLES.has(c.get("role"))) return c.json({ error: "forbidden" }, 403);
    const jobId = c.req.query("job_id") ?? "";
    const workDate = c.req.query("work_date") ?? "";
    if (!DATE_RE.test(workDate)) return c.json({ error: "invalid_work_date" }, 400);
    const jobErr = await requireJob(c, jobId);
    if (jobErr) return jobErr;
    const scopeErr = await requireJobScope(c, jobId, SCOPE_BYPASS_CAPS);
    if (scopeErr) return scopeErr;
    const actor = c.get("session").username;
    let amendsUuid: string | null = null;
    const amendsRaw = c.req.query("amends") ?? "";
    if (amendsRaw !== "" && amendsRaw.length <= 64) {
      const target = await c.env.DB
        .prepare("SELECT actor_username, job_id, work_date FROM submissions WHERE submission_uuid = ?1")
        .bind(amendsRaw)
        .first<{ actor_username: string; job_id: string; work_date: string }>();
      if (target && target.actor_username === actor && target.job_id === jobId && target.work_date === workDate) {
        amendsUuid = amendsRaw;
      }
    }
    // LIMIT: the amend read can carry BOTH the pre-submit unclaimed rows and the amends target's
    // claimed rows (together ≤ POOL_CAP_PER_DAY non-refused) plus refused forensic markers —
    // 2× the cap keeps headroom without unbounding the response.
    const res = await c.env.DB.prepare(
      `SELECT id, status, created_at, screened_at,
              (claimed_by_submission IS NOT NULL) AS claimed
       FROM daily_photo_pool
       WHERE job_id = ?1 AND work_date = ?2 AND uploaded_by = ?3
         AND (claimed_by_submission IS NULL OR (?4 IS NOT NULL AND claimed_by_submission = ?4))
       ORDER BY id ASC LIMIT ${POOL_CAP_PER_DAY * 2}`,
    )
      .bind(jobId, workDate, actor, amendsUuid)
      .all<DailyPoolPhotoRow>();
    return c.json({ photos: res.results ?? [] }, 200);
  });

  // ── POST /api/fieldops/daily-photo/:id/delete — pre-submit removal (own uploads only). ─────────
  // pending|clean + UNCLAIMED only: a claimed row is a filed submission's record linkage (409); a
  // refused row is a byte-free forensic marker and stays (409 — the SPA drops the REF client-side
  // instead). Guard-in-WHERE + conditional audit in ONE batch (W4); the pre-read exists only to
  // name the 4xx (enumeration-safe: a foreign row and a missing row are both 404).
  app.post("/api/fieldops/daily-photo/:id/delete", gates.requireSession, async (c) => {
    if (!DAILY_PHOTO_ROLES.has(c.get("role"))) return c.json({ error: "forbidden" }, 403);
    const id = parseInt(c.req.param("id"), 10);
    if (isNaN(id)) return c.json({ error: "invalid_id" }, 400);
    const actor = c.get("session").username;
    const row = await c.env.DB.prepare(
      "SELECT id, job_id, status, claimed_by_submission FROM daily_photo_pool WHERE id = ?1 AND uploaded_by = ?2",
    )
      .bind(id, actor)
      .first<{ id: number; job_id: string; status: string; claimed_by_submission: string | null }>();
    if (!row) return c.json({ error: "not_found" }, 404);
    if (row.claimed_by_submission !== null) return c.json({ error: "photo_claimed" }, 409);
    if (row.status === "refused") return c.json({ error: "not_deletable" }, 409);

    const res = await c.env.DB.batch([
      c.env.DB
        .prepare(
          "DELETE FROM daily_photo_pool WHERE id = ?1 AND uploaded_by = ?2 AND status IN ('pending','clean') AND claimed_by_submission IS NULL",
        )
        .bind(id, actor),
      auditStmtIfChanged(c, actor, "daily_photo_delete", row.job_id, { pool_id: id, job_id: row.job_id }),
    ]);
    // A lost race (claimed/screened-refused between pre-read and batch) → the guard didn't match;
    // the conditional audit didn't land either (no lying audit row — the W4 discipline).
    if ((res[0].meta.changes ?? 0) === 0) return c.json({ error: "photo_claimed" }, 409);
    return c.json({ ok: true, id }, 200);
  });
}
