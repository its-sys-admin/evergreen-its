/// <reference types="vite/client" />
import type { MiddlewareHandler } from "hono";
import type { FieldopsApp } from "./fieldops_gates";
import type { Env, Vars } from "./types";
import { auditStmt, auditStmtIfChanged, isUniqueViolation } from "./audit";
import { hmacHex } from "./hmac";
import { MAX_ADDRESS } from "./constants";
// SC-S3c wiring — the SC-S2 terms manifest + versioned contractor/payment-terms config, imported at
// BUILD time from subcontracts/ (the same files the Mac renderer reads at render time). A subcontract
// has NO tax table (mirror of po.ts, but the tax.json import is DROPPED); payment_terms.json carries the
// §2.5 retention defaults and is SERVED, not used for math.
import termsManifest from "../../subcontracts/terms/manifest.json";
import contractorConfig from "../../subcontracts/config/contractor.json";
import paymentTermsConfig from "../../subcontracts/config/payment_terms.json";
// SC-S3b Exhibit A — the trade-templated Article II "The Work" config (manifest + per-trade bodies),
// imported at BUILD time so the builder can PRE-FILL Article II from the operator's chosen trade. The
// skeleton (Art I/III/IV/V/VI) is the Mac renderer's concern; the Worker serves only the Art II body a
// Trade maps to. Mirror of the terms edit-text pre-fill pattern below (import.meta.glob + strip header).
import exhibitManifest from "../../subcontracts/exhibit/manifest.json";

// ─────────────────────────────────────────────────────────────────────────────
// Subcontracts workstream SC-S3c — worker/subcontract.ts
//
// The Worker half of the Subcontract-generation pipeline: browser routes (session +
// cap.subcontracts.manage) for the subcontractor cache + subcontract drafts/generate/
// supersede/cancel, and internal routes under the NEW requireSubToken bearer tier
// (PORTAL_SUB_API_TOKEN / Keychain ITS_PORTAL_SUB_TOKEN) that the Mac-side subcontract_poll
// daemon (SC-S3c) consumes.
//
// Invariants:
//   - Invariant 1 (External Send Gate): SEND-FREE — this module performs zero external
//     transmission and has zero AI step. It validates, computes, signs, and queues in D1;
//     the Mac daemon pulls, renders (.docx/.xlsx), files to Box; the SEPARATE subcontract_send
//     (SC-S4) transmits only after F22-verified human approval.
//   - Invariant 2 (Adversarial Input): subcontract drafts arrive from authenticated office
//     admins but are still client-supplied data — every body is shape-guarded + bounded, ALL
//     money is recomputed server-side in integer cents (a client whose displayed contract price
//     disagrees is REJECTED), all SQL is bound, every mutation batches atomically with its audit
//     row (W4), and the queued payload is HMAC-signed under the NEW domain prefix "sub:v1" (same
//     secret as submissions/PO, different domain — a subcontract signature can never replay as a
//     PO or a submission and vice versa).
//   - §51 bidirectional rider (D4): subcontractors is a CACHE of the ITS_Subcontractors SoR.
//     Down-sync is full-replace WITH the dirty-row fence (a sync_state='pending' portal edit is
//     never clobbered) and refuses an empty payload; up-sync is watermarked (mark-mirrored flips
//     pending→synced only if mirror_version is unchanged). NEVER deletes — deactivate only.
//
// MONEY MODEL — a subcontract is a lump-sum CONTRACT PRICE. There is NO tax, NO shipping, NO
// per-watt line. subtotal_cents = Σ sov_lines.extended_cents and MUST equal contract_price_cents
// (the SOV-sums-to-price gate); extended_cents is ALWAYS server-computed round(qty × unit_price_cents).
//
// D7 numbering: sc_number = `${job_no}.${site_phase}.${supersede_seq}.${revision}`, revision allocated
// at generate as MAX(revision)+1 within the family, with the UNIQUE index idx_sc_family_revision
// (migration 0050) as the race backstop.
//
// STATUS machine — draft → queued → pending_review → approved → sent → executed, with superseded /
// canceled off-path. 'executed' (wet-signature countersign terminal) is NEW vs PO.
// ─────────────────────────────────────────────────────────────────────────────

export type SubcontractGates = {
  requireSession: MiddlewareHandler<{ Bindings: Env; Variables: Vars }>;
  requireCapability: (cap: string) => MiddlewareHandler<{ Bindings: Env; Variables: Vars }>;
  /** Bearer gate for /api/subcontracts/internal/* — the Mac-side subcontract_poll daemon's OWN
   *  token tier (PORTAL_SUB_API_TOKEN), privilege-separated from the portal_poll / admin / fieldops
   *  / po / config tokens. Built in index.ts next to its siblings (same fail-closed constant-time
   *  shape); none of those tokens may read this queue and this token may read none of theirs. */
  requireSubToken: MiddlewareHandler<{ Bindings: Env; Variables: Vars }>;
};

const CAP_SUB = "cap.subcontracts.manage";
const SUB_HMAC_DOMAIN = "sub:v1";

// Loose view over the heterogeneous manifest profile shapes (library vs attach).
type TermsProfileEntry = {
  kind: string;
  label: string;
  description: string;
  current_version?: string;
  versions?: Record<string, { file: string; sha256: string; tokens: string[]; legal_review: string }>;
  render_line?: string;
};
const TERMS_PROFILES = termsManifest.profiles as Record<string, TermsProfileEntry>;

// Raw terms clause bodies bundled at BUILD time so the config editor can PRE-FILL a version's
// current text for editing. import.meta.glob auto-discovers every terms *.md, so a version added
// by an add_version actuation is picked up on the next deploy (a static per-file ?raw import would
// silently miss it). Keyed by module path; looked up by the manifest entry's file name.
const TERMS_RAW = import.meta.glob<string>("../../subcontracts/terms/*.md", {
  query: "?raw",
  import: "default",
  eager: true,
});

// Exhibit A per-trade Article II bodies, bundled at BUILD time (same mechanism as TERMS_RAW). Keyed by
// module path; looked up by the manifest trade_template entry's file name — a body added by a new-trade
// actuation is auto-discovered on the next deploy (a static per-file ?raw import would silently miss it).
const EXHIBIT_RAW = import.meta.glob<string>("../../subcontracts/exhibit/art2/*.md", {
  query: "?raw",
  import: "default",
  eager: true,
});

// Trade (the ITS_Subcontractors Trade picklist value) → art2 template key. The manifest fans several
// Trades onto one body (AC/MV/DC Electrical all → 'electrical'); an unknown Trade is invalid_trade.
const EXHIBIT_TRADE_MAP = exhibitManifest.trade_map as Record<string, string>;
const EXHIBIT_TEMPLATES = exhibitManifest.trade_templates as Record<
  string,
  { current_version: string; versions: Record<string, { file: string; sha256: string; legal_review: string }> }
>;

// Port of subcontracts/terms.py header-comment stripping — drop a leading <!-- ... --> provenance
// block (maintainer docs that must never reach a rendered subcontract or the editor textarea). Only
// a comment at the very top is stripped; a malformed unterminated one is served raw (this editor
// pre-fill is a convenience — the Mac renderer's strict loader is the authority and hard-raises).
function stripTermsHeader(text: string): string {
  const s = text.replace(/^\s+/, "");
  if (!s.startsWith("<!--")) return text;
  const end = s.indexOf("-->");
  if (end === -1) return text;
  return s.slice(end + 3).replace(/^\n+/, "");
}

// The default §2.5 retention (bp) a new subcontract inherits — from the versioned config, NOT a
// literal (bump payment_terms.json to change it). 1000 bp = 10%.
const DEFAULT_RETAINAGE_BP: number = paymentTermsConfig.retainage_bp;

// The 51 governing-law jurisdictions (50 states + DC) — the SAME set governing_law.py::_STATE_NAMES
// resolves at render (fails closed on any other value). Served to the jurisdiction picker and used
// as the generate-time fail-closed gate so a subcontract can never queue with an unresolvable state.
const GOVERNING_LAW_STATE_CODES = [
  "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS",
  "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY",
  "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
  "WI", "WY", "DC",
];
const GOVERNING_LAW_STATES = new Set(GOVERNING_LAW_STATE_CODES);

// ── Bounds (Invariant 2) ────────────────────────────────────────────────────────
const MAX_KEY = 64;
const MAX_NAME = 256;
const MAX_SHORT = 64;
const MAX_PHONE = 40;
const MAX_EMAIL = 320;
const MAX_NOTES = 2000;
// Exhibit A "Article II — The Work" is a FULL legal scope body, NOT a PO line-item blurb — it is sized to
// the config-editor's TEMPLATE authoring cap (config.ts MAX_PAYLOAD_BYTES / config_apply _MAX_TERMS_TEXT =
// 100_000), so any template the operator can author pre-fills + saves as an instance. (The 8000 inherited
// from PO's MAX_SOW under-sized it: the electrical Article II is ~20k chars and was outright unsaveable.)
// UNITS: this cap is JS .length (UTF-16 code units); MAX_PAYLOAD_BYTES is UTF-8 bytes; _MAX_TERMS_TEXT is
// Python code points. They coincide for BMP/Latin legal prose (every real Article II body), and since UTF-8
// bytes >= UTF-16 code units, a byte-capped authorable template always fits this instance cap.
const MAX_SCOPE = 100_000;
const MAX_LICENSE = 64;
const MAX_CATEGORIES = 20;
const MAX_LINES = 100;
const MAX_LINE_TEXT = 512;
const MAX_QTY = 1_000_000_000;
const MAX_MONEY_CENTS = 1_000_000_000_000; // $10B — generous ceiling on any single money value
const MAX_SYNC_ROWS = 5000;
const SC_PENDING_CAP = 50;
const SC_STATUS_SYNC_CAP = 200;
const SUB_PENDING_CAP = 200;
const LIST_CAP = 200;

const SUB_KEY_RE = /^SUB-\d{6}$/; // mirrors subcontractors.py _is_valid_sub_key
const JOB_NO_RE = /^\d{4}\.\d{3}$/; // the Evergreen '{YYYY.NNN}' job number (D7)
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const STATE_RE = /^[A-Z]{2}$/;

const PRICE_BASES = new Set(["fixed", "not_to_exceed"]); // 0050 CHECK
const TEMPLATE_FAMILIES = new Set(["long_form", "short_form"]); // 0050 CHECK; default long_form
const SC_STATUSES = new Set([
  "draft", "queued", "pending_review", "approved", "sent", "executed", "superseded", "canceled",
]);
// The statuses the Mac-side status-sync may stamp (draft/queued/pending_review/canceled are
// Worker-owned transitions; the daemon reports only the Mac-side machine's outcomes). 'executed'
// is NEW vs PO — the wet-signature countersign terminal.
const SYNCABLE_STATUSES = new Set(["approved", "sent", "executed", "superseded"]);

// System actor for the token-gated internal routes (no session).
const SYSTEM_ACTOR = "system:subcontract_poll";

// ── Small helpers ───────────────────────────────────────────────────────────────
function str(v: unknown): string {
  return typeof v === "string" ? v.trim() : "";
}
function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}
function isCents(v: unknown): v is number {
  return typeof v === "number" && Number.isSafeInteger(v) && v >= 0 && v <= MAX_MONEY_CENTS;
}
function parseIdParam(raw: string | undefined): number | null {
  const id = parseInt(raw ?? "", 10);
  return Number.isSafeInteger(id) && id > 0 && String(id) === (raw ?? "") ? id : null;
}
function parseJsonArray(v: unknown): string[] {
  if (typeof v !== "string" || !v) return [];
  try {
    const a = JSON.parse(v);
    return Array.isArray(a) ? a.filter((x) => typeof x === "string") : [];
  } catch {
    return [];
  }
}

// ── Subcontractor field validation (create + update + down-sync share it) ────────
interface SubcontractorFields {
  sub_name: string;
  address: string;
  contact_name: string;
  contact_email: string;
  contact_phone: string;
  state: string;
  trades: string; // stored JSON text
  default_terms_profile: string;
  msa_reference: string;
  coi_reference: string;
  license_number: string;
  active: number;
  notes: string;
}

function parseSubcontractorFields(body: Record<string, unknown>): SubcontractorFields | string {
  const sub_name = str(body.sub_name);
  if (sub_name.length < 1 || sub_name.length > MAX_NAME) return "invalid_sub_name";
  const address = str(body.address);
  if (address.length > MAX_ADDRESS) return "invalid_address";
  const contact_name = str(body.contact_name);
  if (contact_name.length > MAX_NAME) return "invalid_contact_name";
  const contact_email = str(body.contact_email);
  if (contact_email.length > MAX_EMAIL || (contact_email && !EMAIL_RE.test(contact_email))) return "invalid_contact_email";
  const contact_phone = str(body.contact_phone);
  if (contact_phone.length > MAX_PHONE) return "invalid_contact_phone";
  // state: 2-letter USPS (jurisdiction grouping). Optional/blank OK (ships dark); if present it must
  // be well-formed. Real-state resolution is the render-time authority (governing_law.resolve).
  const state = str(body.state).toUpperCase();
  if (state && !STATE_RE.test(state)) return "invalid_state";
  let trades: string[] = [];
  if (body.trades !== undefined && body.trades !== null) {
    if (!Array.isArray(body.trades) || body.trades.length > MAX_CATEGORIES) return "invalid_trades";
    for (const s of body.trades) {
      if (typeof s !== "string" || s.length < 1 || s.length > MAX_SHORT) return "invalid_trades";
    }
    trades = body.trades;
  }
  const default_terms_profile = str(body.default_terms_profile);
  if (default_terms_profile.length > MAX_SHORT) return "invalid_default_terms_profile";
  // msa_reference: negotiated Master Subcontract Agreement pointer (attach-not-generate role).
  const msa_reference = str(body.msa_reference);
  if (msa_reference.length > MAX_NAME) return "invalid_msa_reference";
  // coi_reference: insurance/COI evidence POINTER only — NO compliance gate (unseen SoR, ADR-0003 §6).
  const coi_reference = str(body.coi_reference);
  if (coi_reference.length > MAX_NAME) return "invalid_coi_reference";
  const license_number = str(body.license_number);
  if (license_number.length > MAX_LICENSE) return "invalid_license_number";
  const notes = str(body.notes);
  if (notes.length > MAX_NOTES) return "invalid_notes";
  // active: optional (default 1 / keep-live); accepts 0|1|boolean. NEVER a delete — the
  // deactivate-not-delete path (D4) rides this flag.
  let active = 1;
  if (body.active !== undefined && body.active !== null) {
    if (body.active === 0 || body.active === false) active = 0;
    else if (body.active === 1 || body.active === true) active = 1;
    else return "invalid_active";
  }
  return {
    sub_name, address, contact_name, contact_email, contact_phone, state,
    trades: JSON.stringify(trades),
    default_terms_profile, msa_reference, coi_reference, license_number, active, notes,
  };
}

// ── Schedule-of-Values line validation/computation ──────────────────────────────
export interface SovLine {
  position: number;
  item_number: string;
  description: string;
  qty: number;
  unit: string;
  unit_price_cents: number | null;
  extended_cents: number;
}

/** Server-side extended-cents for one SOV line — INTEGER MATH ONLY on money: round(qty × unit_price).
 *  ECMA Math.round is half-up and MUST agree bit-for-bit with money.py::_js_round or the JS/Python
 *  HMAC recompute breaks (money.py docstring is explicit about this). Exported so vitest pins the exact
 *  rounding the HMAC covers. */
export function sovExtendedCents(l: { qty: number; unit_price_cents: number | null }): number {
  return Math.round(l.qty * (l.unit_price_cents ?? 0));
}

/** Parse + bound the client SOV array; positions are SERVER-assigned (1-based array order) and
 *  extended_cents is SERVER-computed — the client's opinion of either is ignored. Every line MUST
 *  carry unit_price_cents (decision #7 — the Worker never trusts a client-supplied extended_cents;
 *  the degenerate corpus single line is {qty:1, unit_price_cents: contract_price_cents}). */
function parseSovLines(raw: unknown): SovLine[] | string {
  if (!Array.isArray(raw) || raw.length < 1 || raw.length > MAX_LINES) return "invalid_sov_lines";
  const out: SovLine[] = [];
  for (let i = 0; i < raw.length; i++) {
    const r = raw[i];
    if (!isPlainObject(r)) return "invalid_sov_lines";
    const item_number = str(r.item_number);
    if (item_number.length > MAX_SHORT) return "invalid_item_number";
    const description = str(r.description);
    if (description.length < 1 || description.length > MAX_LINE_TEXT) return "invalid_description";
    const unit = str(r.unit);
    if (unit.length > 32) return "invalid_unit";
    // qty: finite, bounded, ≤3 decimal places (normalized) so the canonical-JSON HMAC serialization
    // is bit-stable across the JS/Python recompute (shortest-roundtrip doubles agree for ≤3dp).
    const qtyRaw = r.qty;
    if (typeof qtyRaw !== "number" || !Number.isFinite(qtyRaw) || qtyRaw < 0 || qtyRaw > MAX_QTY) return "invalid_qty";
    const qty = Math.round(qtyRaw * 1000) / 1000;
    // unit_price_cents REQUIRED on every line (decision #7): all money server-derived (Invariant 2).
    if (r.unit_price_cents === undefined || r.unit_price_cents === null) return "unit_price_required";
    if (!isCents(r.unit_price_cents)) return "invalid_unit_price_cents";
    const line: SovLine = {
      position: i + 1, item_number, description, qty, unit,
      unit_price_cents: r.unit_price_cents, extended_cents: 0,
    };
    line.extended_cents = sovExtendedCents(line);
    if (line.extended_cents > MAX_MONEY_CENTS) return "line_total_overflow";
    out.push(line);
  }
  return out;
}

/** Recompute the SOV subtotal (Σ extended) from server-validated lines. There is NO tax/shipping/
 *  total — a subcontract is a lump-sum contract price. Exported so vitest pins the math the
 *  sums-to-price assert + HMAC cover. Returns an error code string on overflow. */
export function computeSubtotal(lines: SovLine[]): number | string {
  let subtotal = 0;
  for (const l of lines) subtotal += l.extended_cents;
  if (subtotal > MAX_MONEY_CENTS) return "subtotal_overflow";
  return subtotal;
}

// ── Draft body validation ───────────────────────────────────────────────────────
interface DraftFields {
  job_no: string;
  site_phase: number;
  job_id: string;
  job_name: string;
  project_name: string;
  owner_entity: string;
  prime_contractor: string;
  site_name: string;
  site_address: string;
  governing_law_state: string;
  sub_key: string;
  trade: string;
  exhibit_a_template_id: string;
  exhibit_a_template_version: string;
  exhibit_a_work_text: string;
  scope_summary: string;
  price_basis: string;
  contract_price_cents: number;
  retainage_bp: number;
  start_date: string;
  completion_date: string;
  terms_profile_id: string;
  terms_version: string;
  template_family: string;
  approver_name: string;
  approver_title: string;
  sov_lines: SovLine[];
  subtotal_cents: number;
}

function parseDraftBody(body: Record<string, unknown>): DraftFields | string {
  const sub_key = str(body.sub_key);
  if (!SUB_KEY_RE.test(sub_key)) return "invalid_sub_key";
  const job_no = str(body.job_no);
  if (!JOB_NO_RE.test(job_no)) return "invalid_job_no";
  const site_phase = body.site_phase;
  if (typeof site_phase !== "number" || !Number.isSafeInteger(site_phase) || site_phase < 0 || site_phase > 9999) {
    return "invalid_site_phase";
  }
  const job_id = str(body.job_id);
  if (job_id.length > MAX_KEY) return "invalid_job_id";
  const job_name = str(body.job_name);
  if (job_name.length > MAX_NAME) return "invalid_job_name";
  const project_name = str(body.project_name);
  if (project_name.length > MAX_NAME) return "invalid_project_name";
  const owner_entity = str(body.owner_entity);
  if (owner_entity.length > MAX_NAME) return "invalid_owner_entity";
  const prime_contractor = str(body.prime_contractor);
  if (prime_contractor.length > MAX_NAME) return "invalid_prime_contractor";
  const site_name = str(body.site_name);
  if (site_name.length > MAX_NAME) return "invalid_site_name";
  const site_address = str(body.site_address);
  if (site_address.length > MAX_ADDRESS) return "invalid_site_address";
  // governing_law_state: format-validated when present. NOT required non-blank at draft (the render's
  // governing_law.resolve is the fail-closed authority; the generate route re-checks it against the
  // real-state set so a queued row can always render).
  const governing_law_state = str(body.governing_law_state).toUpperCase();
  if (governing_law_state && !STATE_RE.test(governing_law_state)) return "invalid_governing_law_state";
  const trade = str(body.trade);
  if (trade.length > MAX_SHORT) return "invalid_trade";
  const exhibit_a_template_id = str(body.exhibit_a_template_id);
  if (exhibit_a_template_id.length > MAX_SHORT) return "invalid_exhibit_a_template_id";
  const exhibit_a_template_version = str(body.exhibit_a_template_version);
  if (exhibit_a_template_version.length > 32) return "invalid_exhibit_a_template_version";
  const exhibit_a_work_text = str(body.exhibit_a_work_text);
  if (exhibit_a_work_text.length > MAX_SCOPE) return "invalid_exhibit_a_work_text";
  const scope_summary = str(body.scope_summary);
  if (scope_summary.length > MAX_LINE_TEXT) return "invalid_scope_summary";
  const price_basis = str(body.price_basis) || "fixed";
  if (!PRICE_BASES.has(price_basis)) return "invalid_price_basis";
  if (!isCents(body.contract_price_cents)) return "invalid_contract_price";
  const contract_price_cents = body.contract_price_cents;
  // retainage_bp: optional int 0..10000; default the §2.5 config default (payment_terms.json).
  let retainage_bp = DEFAULT_RETAINAGE_BP;
  if (body.retainage_bp !== undefined && body.retainage_bp !== null) {
    const bp = body.retainage_bp;
    if (typeof bp !== "number" || !Number.isSafeInteger(bp) || bp < 0 || bp > 10_000) return "invalid_retainage_bp";
    retainage_bp = bp;
  }
  const start_date = str(body.start_date);
  if (start_date.length > 32) return "invalid_start_date";
  const completion_date = str(body.completion_date);
  if (completion_date.length > 32) return "invalid_completion_date";
  // terms_profile_id: default 'standard_subcontract' (decision #5) so D1 never stores '' (a '' would
  // TermsError-fence at render).
  const terms_profile_id = str(body.terms_profile_id) || "standard_subcontract";
  if (terms_profile_id.length > MAX_SHORT) return "invalid_terms_profile";
  const terms_version = str(body.terms_version);
  if (terms_version.length > 32) return "invalid_terms_version";
  const template_family = str(body.template_family) || "long_form";
  if (!TEMPLATE_FAMILIES.has(template_family)) return "invalid_template_family";
  const approver_name = str(body.approver_name);
  const approver_title = str(body.approver_title);
  if (approver_name.length > MAX_NAME || approver_title.length > MAX_NAME) return "invalid_approver";

  // SOV: an omitted/empty sov_lines auto-echoes the single-line derivation the corpus uses
  // (subtotal == contract_price by construction); a supplied set is validated + must sum to price.
  const rawSov = Array.isArray(body.sov_lines) && body.sov_lines.length > 0
    ? body.sov_lines
    : [{
        position: 1, item_number: "", description: scope_summary || "Contract Sum",
        qty: 1, unit: "", unit_price_cents: contract_price_cents, extended_cents: contract_price_cents,
      }];
  const sov_lines = parseSovLines(rawSov);
  if (typeof sov_lines === "string") return sov_lines;
  const subtotal = computeSubtotal(sov_lines);
  if (typeof subtotal === "string") return subtotal;
  // The sums-to-price gate at draft — a draft that can never generate fails early (mirroring PO's
  // auto-tax early fail).
  if (subtotal !== contract_price_cents) return "sov_mismatch";

  return {
    job_no, site_phase, job_id, job_name,
    project_name, owner_entity, prime_contractor, site_name, site_address, governing_law_state,
    sub_key, trade,
    exhibit_a_template_id, exhibit_a_template_version, exhibit_a_work_text, scope_summary,
    price_basis, contract_price_cents, retainage_bp,
    start_date, completion_date,
    terms_profile_id, terms_version, template_family,
    approver_name, approver_title,
    sov_lines, subtotal_cents: subtotal,
  };
}

// ── HMAC canonical payload (domain 'sub:v1') ─────────────────────────────────────
// The Mac-side subcontract_poll daemon (shared/portal_hmac.py SUB_DOMAIN) recomputes this
// byte-for-byte before rendering/filing. ORDER + SEPARATOR are load-bearing:
//   "sub:v1" \n sc_id \n sc_number \n canonical_payload_json
// canonical_payload_json is JSON.stringify of the FIXED-KEY-ORDER object below (insertion order
// preserved; the Python side mirrors with json.dumps(..., separators=(",",":")) over the same key
// order — the 31-key header tuple pinned in the SC-S3c build contract). All money/count values are
// integers; qty is a ≤3dp double whose shortest-roundtrip serialization agrees across JS/Python.
export interface SubcontractRow {
  id: number;
  sc_number: string | null;
  job_no: string;
  site_phase: number;
  supersede_seq: number;
  revision: number | null;
  sub_key: string;
  trade: string;
  job_id: string;
  job_name: string;
  project_name: string;
  owner_entity: string;
  prime_contractor: string;
  site_name: string;
  site_address: string;
  governing_law_state: string;
  exhibit_a_template_id: string;
  exhibit_a_template_version: string;
  exhibit_a_work_text: string;
  scope_summary: string;
  price_basis: string;
  contract_price_cents: number;
  retainage_bp: number;
  subtotal_cents: number;
  start_date: string;
  completion_date: string;
  terms_profile_id: string;
  terms_version: string;
  template_family: string;
  supersedes_sc_id: number | null;
  approver_name: string;
  approver_title: string;
}

export function canonicalSubJson(sub: SubcontractRow, lines: SovLine[]): string {
  return JSON.stringify({
    sc_number: sub.sc_number,
    job_no: sub.job_no,
    site_phase: sub.site_phase,
    supersede_seq: sub.supersede_seq,
    revision: sub.revision,
    sub_key: sub.sub_key,
    trade: sub.trade,
    job_id: sub.job_id,
    job_name: sub.job_name,
    project_name: sub.project_name,
    owner_entity: sub.owner_entity,
    prime_contractor: sub.prime_contractor,
    site_name: sub.site_name,
    site_address: sub.site_address,
    governing_law_state: sub.governing_law_state,
    exhibit_a_template_id: sub.exhibit_a_template_id,
    exhibit_a_template_version: sub.exhibit_a_template_version,
    exhibit_a_work_text: sub.exhibit_a_work_text,
    scope_summary: sub.scope_summary,
    price_basis: sub.price_basis,
    contract_price_cents: sub.contract_price_cents,
    retainage_bp: sub.retainage_bp,
    subtotal_cents: sub.subtotal_cents,
    start_date: sub.start_date,
    completion_date: sub.completion_date,
    terms_profile_id: sub.terms_profile_id,
    terms_version: sub.terms_version,
    template_family: sub.template_family,
    supersedes_sc_id: sub.supersedes_sc_id,
    approver_name: sub.approver_name,
    approver_title: sub.approver_title,
    sov_lines: lines.map((l) => ({
      position: l.position,
      item_number: l.item_number,
      description: l.description,
      qty: l.qty,
      unit: l.unit,
      unit_price_cents: l.unit_price_cents,
      extended_cents: l.extended_cents,
    })),
  });
}

export function subCanonicalString(scId: number, scNumber: string, canonicalJson: string): string {
  return [SUB_HMAC_DOMAIN, String(scId), scNumber, canonicalJson].join("\n");
}

// The SOV columns every read path selects (one definition, N readers — no drift).
const SOV_COLS = "position, item_number, description, qty, unit, unit_price_cents, extended_cents";

async function loadSovLines(db: D1Database, scId: number): Promise<SovLine[]> {
  const { results } = await db
    .prepare(`SELECT ${SOV_COLS} FROM sov_lines WHERE subcontract_id = ?1 ORDER BY position ASC`)
    .bind(scId)
    .all<SovLine>();
  return (results ?? []) as SovLine[];
}

// ── Route registration ──────────────────────────────────────────────────────────
export function registerSubcontractRoutes(app: FieldopsApp, gates: SubcontractGates): void {
  // ══ Browser surface (session + cap.subcontracts.manage) ═════════════════════════

  // GET /api/subcontracts/terms — the terms-library picker feed (SC-S2 manifest, build-time import).
  // Serves the SPA a curated view: profile id/kind/label + the current version + its tokens (library)
  // or the render line (attach) — never the raw manifest (hash pins and file names are renderer
  // implementation detail, not picker data).
  app.get("/api/subcontracts/terms", gates.requireSession, gates.requireCapability(CAP_SUB), (c) => {
    const profiles = Object.entries(TERMS_PROFILES).map(([id, p]) => ({
      id,
      kind: p.kind,
      label: p.label,
      description: p.description,
      current_version: p.current_version ?? null,
      tokens: (p.current_version && p.versions?.[p.current_version]?.tokens) || [],
      render_line: p.render_line ?? null,
    }));
    return c.json({ profiles });
  });

  // GET /api/subcontracts/terms/:profile_id/text — the CURRENT version's clause BODY (header-stripped),
  // for the config editor's "edit text" pre-fill. Library profiles only. Read-only, no audit.
  app.get(
    "/api/subcontracts/terms/:profile_id/text",
    gates.requireSession,
    gates.requireCapability(CAP_SUB),
    (c) => {
      const profileId = c.req.param("profile_id");
      // Own-property lookup only — a path param like __proto__/constructor must not resolve to an
      // Object.prototype built-in (defense-in-depth; the kind check below already 404s such keys).
      if (!Object.prototype.hasOwnProperty.call(TERMS_PROFILES, profileId)) {
        return c.json({ error: "unknown_profile" }, 404);
      }
      const p = TERMS_PROFILES[profileId];
      if (p.kind !== "library" || !p.current_version || !p.versions) {
        return c.json({ error: "no_editable_text" }, 404);
      }
      const entry = p.versions[p.current_version];
      if (!entry) return c.json({ error: "no_current_version" }, 404);
      const key = Object.keys(TERMS_RAW).find((k) => k.endsWith("/" + entry.file));
      if (key === undefined) return c.json({ error: "text_unavailable" }, 404);
      return c.json({
        profile_id: profileId,
        version: p.current_version,
        text: stripTermsHeader(TERMS_RAW[key]),
      });
    },
  );

  // GET /api/subcontracts/terms/:profile_id/versions — the version list for a library profile, so the
  // config editor's "make current" picker shows every version + its legal_review status + which is
  // current. CURATED: version id + legal_review only — file names / sha256 stay off the wire.
  app.get(
    "/api/subcontracts/terms/:profile_id/versions",
    gates.requireSession,
    gates.requireCapability(CAP_SUB),
    (c) => {
      const profileId = c.req.param("profile_id");
      if (!Object.prototype.hasOwnProperty.call(TERMS_PROFILES, profileId)) {
        return c.json({ error: "unknown_profile" }, 404);
      }
      const p = TERMS_PROFILES[profileId];
      if (p.kind !== "library" || !p.versions) {
        return c.json({ error: "no_versions" }, 404);
      }
      const versions = Object.entries(p.versions).map(([version, entry]) => ({
        version,
        legal_review: entry.legal_review,
      }));
      return c.json({ profile_id: profileId, current_version: p.current_version ?? null, versions });
    },
  );

  // GET /api/subcontracts/config — the versioned contractor identity + §2.5 payment-terms defaults +
  // the governing-law state list for the builder UI. Explicit key picks — the JSON files carry
  // maintainer comment/config_version fields that don't belong on the wire. NO tax (subcontracts have
  // no tax table).
  app.get("/api/subcontracts/config", gates.requireSession, gates.requireCapability(CAP_SUB), (c) =>
    c.json({
      contractor: {
        entity: contractorConfig.entity,
        address_lines: contractorConfig.address_lines,
        phone: contractorConfig.phone,
        signature_entity: contractorConfig.signature_entity,
        prime_contractor_default: contractorConfig.prime_contractor_default,
      },
      payment_terms: {
        retainage_bp: paymentTermsConfig.retainage_bp,
        retainage_reduced_bp: paymentTermsConfig.retainage_reduced_bp,
        retainage_reduction_at_pct: paymentTermsConfig.retainage_reduction_at_pct,
      },
      governing_law_states: GOVERNING_LAW_STATE_CODES,
    }),
  );

  // GET /api/subcontracts/trades — the operator-selectable Trade vocabulary for the subcontract builder
  // and the Subcontractors trade-tagging picker. DERIVED from the manifest trade_map keys so a trade added
  // via the config editor (exhibit create_profile) surfaces here with NO SPA code edit — the manifest is
  // the single source of truth for "what trades exist" that the renderer also resolves against. Insertion
  // order preserved (the curated grouping; a newly-created trade appends last). Read-only, no audit; same
  // session + cap gate as the sibling exhibit routes. The SPA keeps a static fallback for a degraded fetch.
  app.get("/api/subcontracts/trades", gates.requireSession, gates.requireCapability(CAP_SUB), (c) =>
    c.json({ trades: Object.keys(EXHIBIT_TRADE_MAP) }),
  );

  // GET /api/subcontracts/exhibit-templates?trade=<Trade> — the Exhibit A Article II ("The Work")
  // pre-fill: resolve the operator-picked Trade through the manifest trade_map to its art2 template key,
  // return that trade's standard Art II body (header-stripped) as an editable starting point for
  // exhibit_a_work_text. Several Trades fan onto one body (AC/MV/DC Electrical → 'electrical'); an
  // unknown Trade is 400 invalid_trade. Read-only, no audit — same session + cap gate as the browser
  // routes above. The Mac renderer's strict loader is the authority; this is a builder convenience.
  app.get("/api/subcontracts/exhibit-templates", gates.requireSession, gates.requireCapability(CAP_SUB), (c) => {
    const trade = str(c.req.query("trade"));
    // Own-property lookup only — a query like __proto__/constructor must not resolve to an
    // Object.prototype built-in (defense-in-depth; the resolved key is re-checked below).
    if (!trade || !Object.prototype.hasOwnProperty.call(EXHIBIT_TRADE_MAP, trade)) {
      return c.json({ error: "invalid_trade" }, 400);
    }
    const templateKey = EXHIBIT_TRADE_MAP[trade];
    const tmpl = Object.prototype.hasOwnProperty.call(EXHIBIT_TEMPLATES, templateKey)
      ? EXHIBIT_TEMPLATES[templateKey]
      : undefined;
    if (!tmpl) return c.json({ error: "invalid_trade" }, 400);
    // Resolve the key's CURRENT (legal-cleared) version → its immutable file (PR-B2 versioned schema).
    const ver = tmpl.versions?.[tmpl.current_version];
    if (!ver) return c.json({ error: "template_unavailable" }, 404);
    const key = Object.keys(EXHIBIT_RAW).find((k) => k.endsWith("/" + ver.file));
    if (key === undefined) return c.json({ error: "template_unavailable" }, 404);
    return c.json({
      trade,
      template_key: templateKey,
      article_ii: stripTermsHeader(EXHIBIT_RAW[key]),
    });
  });

  // ── Config-editor exhibit routes (PR-B2) — the per-trade Article II template picker/editor, keyed by
  //    template KEY (not Trade). Session + cap.subcontracts.manage; read-only; the Mac config actuator
  //    is the write path (POST /api/config/requests, artifact 'exhibit').
  // GET /api/subcontracts/exhibit-keys — every template key + current_version + versions (legal_review)
  // + the Trades that map to it. Metadata only (no bodies).
  app.get("/api/subcontracts/exhibit-keys", gates.requireSession, gates.requireCapability(CAP_SUB), (c) => {
    const tradesFor: Record<string, string[]> = {};
    for (const [trade, key] of Object.entries(EXHIBIT_TRADE_MAP)) {
      (tradesFor[key] ??= []).push(trade);
    }
    const templates = Object.keys(EXHIBIT_TEMPLATES)
      .sort()
      .map((key) => {
        const tmpl = EXHIBIT_TEMPLATES[key];
        return {
          template_key: key,
          current_version: tmpl.current_version,
          trades: (tradesFor[key] ?? []).sort(),
          versions: Object.keys(tmpl.versions ?? {})
            .sort()
            .map((v) => ({ version: v, legal_review: tmpl.versions[v]?.legal_review ?? "" })),
        };
      });
    return c.json({ templates });
  });

  // GET /api/subcontracts/exhibit-keys/:key/text?version=<v> — the Article II body for a KEY at its
  // current (or an explicit) version, header-stripped, for the editor's 'edit from live' pre-fill.
  app.get(
    "/api/subcontracts/exhibit-keys/:key/text",
    gates.requireSession,
    gates.requireCapability(CAP_SUB),
    (c) => {
      const key = c.req.param("key");
      const tmpl = Object.prototype.hasOwnProperty.call(EXHIBIT_TEMPLATES, key) ? EXHIBIT_TEMPLATES[key] : undefined;
      if (!tmpl) return c.json({ error: "unknown_key" }, 404);
      const version = str(c.req.query("version")) || tmpl.current_version;
      // Own-property lookup only — a version like __proto__/constructor must not resolve to an
      // Object.prototype built-in (defense-in-depth, matching the :key / ?trade= lookups in this file).
      const ver = Object.prototype.hasOwnProperty.call(tmpl.versions ?? {}, version)
        ? tmpl.versions[version]
        : undefined;
      if (!ver) return c.json({ error: "unknown_version" }, 404);
      const raw = Object.keys(EXHIBIT_RAW).find((k) => k.endsWith("/" + ver.file));
      if (raw === undefined) return c.json({ error: "text_unavailable" }, 404);
      return c.json({ template_key: key, version, article_ii: stripTermsHeader(EXHIBIT_RAW[raw]) });
    },
  );

  // GET /api/subcontracts/exhibit-keys/:key/versions — a KEY's versions + legal_review + current_version
  // (the editor's make-current picker source).
  app.get(
    "/api/subcontracts/exhibit-keys/:key/versions",
    gates.requireSession,
    gates.requireCapability(CAP_SUB),
    (c) => {
      const key = c.req.param("key");
      const tmpl = Object.prototype.hasOwnProperty.call(EXHIBIT_TEMPLATES, key) ? EXHIBIT_TEMPLATES[key] : undefined;
      if (!tmpl) return c.json({ error: "unknown_key" }, 404);
      const versions = Object.keys(tmpl.versions ?? {})
        .sort()
        .map((v) => ({ version: v, legal_review: tmpl.versions[v]?.legal_review ?? "" }));
      return c.json({ template_key: key, current_version: tmpl.current_version, versions });
    },
  );

  // GET /api/subcontracts/jobs/:job_id/site-address — the builder's Site-address auto-fill (C1),
  // mirroring PO's /api/po/jobs/:job_id/ship-to. Reads the SAME jobs.address the Smartsheet
  // ITS_Active_Jobs "Address" SoR syncs down (portal_poll → /api/internal/sync). Under the browser
  // session + cap.subcontracts.manage gate; READ-ONLY, bound single-PK lookup, no mutation. These are
  // Evergreen's OWN job-site addresses (not third-party PII). Auto-fill is a CONVENIENCE — a 404 /
  // absent / blank address silently leaves the operator-editable Site-address field alone.
  app.get(
    "/api/subcontracts/jobs/:job_id/site-address",
    gates.requireSession,
    gates.requireCapability(CAP_SUB),
    async (c) => {
      const jobId = c.req.param("job_id");
      const row = await c.env.DB
        .prepare("SELECT job_id, address FROM jobs WHERE job_id = ?1")
        .bind(jobId)
        .first<{ job_id: string; address: string }>();
      if (!row) return c.json({ error: "not_found" }, 404);
      return c.json({ job_id: row.job_id, site_address: row.address ?? "" });
    },
  );

  // GET /api/subcontracts/subcontractors — the subcontractor picker/management read. Active-only by
  // default; ?include_inactive=1 widens (the management list shows retired subcontractors greyed).
  app.get("/api/subcontracts/subcontractors", gates.requireSession, gates.requireCapability(CAP_SUB), async (c) => {
    const includeInactive = c.req.query("include_inactive") === "1";
    const { results } = await c.env.DB
      .prepare(
        "SELECT sub_key, sub_name, address, contact_name, contact_email, contact_phone, " +
          "state, trades, default_terms_profile, msa_reference, coi_reference, license_number, " +
          "active, notes, origin, sync_state, mirror_version " +
          "FROM subcontractors WHERE (?1 = 1 OR active = 1) ORDER BY sub_name ASC",
      )
      .bind(includeInactive ? 1 : 0)
      .all<Record<string, unknown>>();
    const subcontractors = (results ?? []).map((v) => ({ ...v, trades: parseJsonArray(v.trades) }));
    return c.json({ subcontractors });
  });

  // POST /api/subcontracts/subcontractors — portal subcontractor create (§51 rider, D4). Allocates the
  // next SUB-###### atomically: the single UPDATE..RETURNING both self-heals past the max suffix already
  // present (a key that arrived via down-sync) AND increments — D1 serializes writes, so two concurrent
  // creates get distinct keys; the PK is the backstop (isUniqueViolation → 409). origin='portal' +
  // sync_state='pending' + mirror_version=1 makes the new row immediately dirty for the up-sync.
  app.post("/api/subcontracts/subcontractors", gates.requireSession, gates.requireCapability(CAP_SUB), async (c) => {
    let body: Record<string, unknown>;
    try {
      body = (await c.req.json()) as Record<string, unknown>;
    } catch {
      return c.json({ error: "bad_request" }, 400);
    }
    if (!isPlainObject(body)) return c.json({ error: "bad_request" }, 400);
    const f = parseSubcontractorFields(body);
    if (typeof f === "string") return c.json({ error: f }, 400);

    let counter: { last_value: number } | null;
    try {
      counter = await c.env.DB
        .prepare(
          "UPDATE subcontractor_counter SET last_value = MAX(last_value, COALESCE((" +
            "SELECT MAX(CAST(substr(sub_key, 5) AS INTEGER)) FROM subcontractors WHERE sub_key LIKE 'SUB-______'" +
            "), 0)) + 1 WHERE id = 1 RETURNING last_value",
        )
        .first<{ last_value: number }>();
    } catch {
      counter = null; // table absent (0049 not applied) → fail closed, same as a missing seed row
    }
    if (!counter) return c.json({ error: "counter_unavailable" }, 500);
    const subKey = `SUB-${String(counter.last_value).padStart(6, "0")}`;

    const actor = c.get("session").username;
    try {
      await c.env.DB.batch([
        c.env.DB
          .prepare(
            "INSERT INTO subcontractors (sub_key, sub_name, address, contact_name, contact_email, " +
              "contact_phone, state, trades, default_terms_profile, msa_reference, coi_reference, " +
              "license_number, active, notes, origin, sync_state, mirror_version, mirrored_version) " +
              "VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,'portal','pending',1,0)",
          )
          .bind(
            subKey, f.sub_name, f.address, f.contact_name, f.contact_email, f.contact_phone,
            f.state, f.trades, f.default_terms_profile, f.msa_reference, f.coi_reference,
            f.license_number, f.active, f.notes,
          ),
        auditStmt(c, actor, "sc_subcontractor_create", subKey, { sub_key: subKey, sub_name: f.sub_name }),
      ]);
    } catch (e) {
      if (isUniqueViolation(e)) return c.json({ error: "subcontractor_exists" }, 409);
      throw e;
    }
    return c.json({ ok: true, sub_key: subKey }, 201);
  });

  // POST /api/subcontracts/subcontractors/:sub_key/update — full-field portal edit (deactivation rides
  // `active: 0` — NEVER a delete, D4). Re-dirties the row for the up-sync: origin='portal',
  // sync_state='pending', mirror_version+1 — the bump is what invalidates a racing mark-mirrored's
  // watermark.
  app.post("/api/subcontracts/subcontractors/:sub_key/update", gates.requireSession, gates.requireCapability(CAP_SUB), async (c) => {
    const subKey = c.req.param("sub_key");
    if (!SUB_KEY_RE.test(subKey)) return c.json({ error: "invalid_sub_key" }, 400);
    let body: Record<string, unknown>;
    try {
      body = (await c.req.json()) as Record<string, unknown>;
    } catch {
      return c.json({ error: "bad_request" }, 400);
    }
    if (!isPlainObject(body)) return c.json({ error: "bad_request" }, 400);
    const f = parseSubcontractorFields(body);
    if (typeof f === "string") return c.json({ error: f }, 400);

    const actor = c.get("session").username;
    const res = await c.env.DB.batch([
      c.env.DB
        .prepare(
          "UPDATE subcontractors SET sub_name=?2, address=?3, contact_name=?4, contact_email=?5, " +
            "contact_phone=?6, state=?7, trades=?8, default_terms_profile=?9, msa_reference=?10, " +
            "coi_reference=?11, license_number=?12, active=?13, notes=?14, " +
            "origin='portal', sync_state='pending', mirror_version=mirror_version+1, updated_at=unixepoch() " +
            "WHERE sub_key=?1",
        )
        .bind(
          subKey, f.sub_name, f.address, f.contact_name, f.contact_email, f.contact_phone,
          f.state, f.trades, f.default_terms_profile, f.msa_reference, f.coi_reference,
          f.license_number, f.active, f.notes,
        ),
      auditStmtIfChanged(c, actor, "sc_subcontractor_update", subKey, { sub_key: subKey, active: f.active }),
    ]);
    if ((res[0].meta.changes ?? 0) === 0) return c.json({ error: "not_found" }, 404);
    return c.json({ ok: true, sub_key: subKey });
  });

  // GET /api/subcontracts/subs?status=&limit= — the tracker list (drafts + queued + the rest).
  // Status validated against the fixed vocabulary (+ 'all', the default).
  app.get("/api/subcontracts/subs", gates.requireSession, gates.requireCapability(CAP_SUB), async (c) => {
    const q = c.req.query();
    const status = SC_STATUSES.has(q.status ?? "") ? (q.status as string) : "all";
    const limit = Math.min(Math.max(parseInt(q.limit || "50", 10) || 50, 1), LIST_CAP);
    const { results } = await c.env.DB
      .prepare(
        "SELECT id, sc_number, job_no, site_phase, supersede_seq, revision, sub_key, job_id, job_name, " +
          "project_name, owner_entity, status, contract_price_cents, supersedes_sc_id, change_order_of, co_seq, box_file_id, " +
          "created_by, created_at, updated_at " +
          "FROM subcontracts WHERE (?1 = 'all' OR status = ?1) ORDER BY updated_at DESC, id DESC LIMIT ?2",
      )
      .bind(status, limit)
      .all<Record<string, unknown>>();
    return c.json({ subcontracts: results ?? [] });
  });

  // GET /api/subcontracts/subs/:id — one subcontract + its SOV lines (any status; the SPA detail read).
  app.get("/api/subcontracts/subs/:id", gates.requireSession, gates.requireCapability(CAP_SUB), async (c) => {
    const id = parseIdParam(c.req.param("id"));
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    const subcontract = await c.env.DB.prepare("SELECT * FROM subcontracts WHERE id = ?1").bind(id).first<Record<string, unknown>>();
    if (!subcontract) return c.json({ error: "not_found" }, 404);
    const sov_lines = await loadSovLines(c.env.DB, id);
    return c.json({ subcontract, sov_lines });
  });

  // POST /api/subcontracts/drafts — create a draft. The subcontractor must exist AND be active (422 —
  // shape is fine, the reference isn't). All money server-computed; the stored subtotal is the SERVER's
  // number, whatever the client displayed — the hard assert against the client's contract price happens
  // at generate.
  app.post("/api/subcontracts/drafts", gates.requireSession, gates.requireCapability(CAP_SUB), async (c) => {
    let body: Record<string, unknown>;
    try {
      body = (await c.req.json()) as Record<string, unknown>;
    } catch {
      return c.json({ error: "bad_request" }, 400);
    }
    if (!isPlainObject(body)) return c.json({ error: "bad_request" }, 400);
    const d = parseDraftBody(body);
    if (typeof d === "string") return c.json({ error: d }, 400);

    const subcontractor = await c.env.DB
      .prepare("SELECT sub_key FROM subcontractors WHERE sub_key = ?1 AND active = 1")
      .bind(d.sub_key)
      .first();
    if (!subcontractor) return c.json({ error: "unknown_subcontractor" }, 422);

    const actor = c.get("session").username;
    const scUuid = crypto.randomUUID();
    // Parent + all SOV lines + audit in ONE batch (W4). The line INSERTs resolve subcontract_id via a
    // scalar subquery on sc_uuid — constant during each statement (the parent row landed in statement
    // 1); last_insert_rowid() would MOVE per inserted row and is deliberately avoided.
    const stmts = [
      c.env.DB
        .prepare(
          "INSERT INTO subcontracts (sc_uuid, job_no, site_phase, supersede_seq, job_id, job_name, " +
            "project_name, owner_entity, prime_contractor, site_name, site_address, governing_law_state, " +
            "sub_key, trade, exhibit_a_template_id, exhibit_a_template_version, exhibit_a_work_text, " +
            "scope_summary, price_basis, contract_price_cents, retainage_bp, subtotal_cents, " +
            "start_date, completion_date, terms_profile_id, terms_version, template_family, " +
            "status, approver_name, approver_title, created_by) " +
            "VALUES (?1,?2,?3,0,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17,?18,?19,?20,?21,?22,?23,?24,?25,?26,'draft',?27,?28,?29) " +
            "RETURNING id",
        )
        .bind(
          scUuid, d.job_no, d.site_phase, d.job_id, d.job_name,
          d.project_name, d.owner_entity, d.prime_contractor, d.site_name, d.site_address, d.governing_law_state,
          d.sub_key, d.trade, d.exhibit_a_template_id, d.exhibit_a_template_version, d.exhibit_a_work_text,
          d.scope_summary, d.price_basis, d.contract_price_cents, d.retainage_bp, d.subtotal_cents,
          d.start_date, d.completion_date, d.terms_profile_id, d.terms_version, d.template_family,
          d.approver_name, d.approver_title, actor,
        ),
      ...d.sov_lines.map((l) =>
        c.env.DB
          .prepare(
            `INSERT INTO sov_lines (subcontract_id, ${SOV_COLS}) ` +
              "SELECT (SELECT id FROM subcontracts WHERE sc_uuid = ?1), ?2,?3,?4,?5,?6,?7,?8",
          )
          .bind(
            scUuid, l.position, l.item_number, l.description, l.qty, l.unit, l.unit_price_cents, l.extended_cents,
          ),
      ),
      auditStmt(c, actor, "sc_draft_create", scUuid, {
        sc_uuid: scUuid, job_no: d.job_no, site_phase: d.site_phase, sub_key: d.sub_key,
        contract_price_cents: d.contract_price_cents,
      }),
    ];
    const res = await c.env.DB.batch(stmts);
    const id = (res[0].results?.[0] as { id: number } | undefined)?.id ?? null;
    return c.json({ ok: true, id, subtotal_cents: d.subtotal_cents }, 201);
  });

  // POST /api/subcontracts/drafts/:id/update — full-replace edit, DRAFT-ONLY (guarded in-WHERE; the
  // SOV DELETE/INSERTs are each guarded on the live status so a lost race writes nothing).
  app.post("/api/subcontracts/drafts/:id/update", gates.requireSession, gates.requireCapability(CAP_SUB), async (c) => {
    const id = parseIdParam(c.req.param("id"));
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    let body: Record<string, unknown>;
    try {
      body = (await c.req.json()) as Record<string, unknown>;
    } catch {
      return c.json({ error: "bad_request" }, 400);
    }
    if (!isPlainObject(body)) return c.json({ error: "bad_request" }, 400);
    const d = parseDraftBody(body);
    if (typeof d === "string") return c.json({ error: d }, 400);

    const subcontractor = await c.env.DB
      .prepare("SELECT sub_key FROM subcontractors WHERE sub_key = ?1 AND active = 1")
      .bind(d.sub_key)
      .first();
    if (!subcontractor) return c.json({ error: "unknown_subcontractor" }, 422);

    // Track D2 identity lock (the po.ts twin): a change-order draft amends ONE parent
    // contract — counterparty + job identity are the parent's. Scope, SOV, price, dates and
    // terms stay freely editable; that IS the change order.
    const cur = await c.env.DB
      .prepare("SELECT change_order_of, sub_key, job_no, site_phase, job_id FROM subcontracts WHERE id = ?1")
      .bind(id)
      .first<{ change_order_of: number | null; sub_key: string; job_no: string; site_phase: number; job_id: string }>();
    if (
      cur && cur.change_order_of !== null &&
      (d.sub_key !== cur.sub_key || d.job_no !== cur.job_no || d.site_phase !== cur.site_phase || d.job_id !== cur.job_id)
    ) {
      return c.json({ error: "co_identity_locked" }, 409);
    }

    const actor = c.get("session").username;
    const guard = "(SELECT status FROM subcontracts WHERE id = ?1) = 'draft'";
    const res = await c.env.DB.batch([
      c.env.DB
        .prepare(
          "UPDATE subcontracts SET job_no=?2, site_phase=?3, job_id=?4, job_name=?5, " +
            "project_name=?6, owner_entity=?7, prime_contractor=?8, site_name=?9, site_address=?10, " +
            "governing_law_state=?11, sub_key=?12, trade=?13, exhibit_a_template_id=?14, " +
            "exhibit_a_template_version=?15, exhibit_a_work_text=?16, scope_summary=?17, price_basis=?18, " +
            "contract_price_cents=?19, retainage_bp=?20, subtotal_cents=?21, start_date=?22, " +
            "completion_date=?23, terms_profile_id=?24, terms_version=?25, template_family=?26, " +
            "approver_name=?27, approver_title=?28, updated_at=unixepoch(), " +
            // draft_version covers the WHOLE draft snapshot (this route rewrites parent AND SOV
            // lines together) — generate() pins its status flip on the version it read.
            "draft_version=draft_version+1 " +
            "WHERE id=?1 AND status='draft'",
        )
        .bind(
          id, d.job_no, d.site_phase, d.job_id, d.job_name,
          d.project_name, d.owner_entity, d.prime_contractor, d.site_name, d.site_address,
          d.governing_law_state, d.sub_key, d.trade, d.exhibit_a_template_id,
          d.exhibit_a_template_version, d.exhibit_a_work_text, d.scope_summary, d.price_basis,
          d.contract_price_cents, d.retainage_bp, d.subtotal_cents, d.start_date,
          d.completion_date, d.terms_profile_id, d.terms_version, d.template_family,
          d.approver_name, d.approver_title,
        ),
      auditStmtIfChanged(c, actor, "sc_draft_update", String(id), { sc_id: id, contract_price_cents: d.contract_price_cents }),
      // Full-replace the SOV set — status-guarded so a non-draft row's lines are untouched even if
      // this batch raced the generate (status is not modified above, so the subquery reflects the
      // true row state).
      c.env.DB.prepare(`DELETE FROM sov_lines WHERE subcontract_id = ?1 AND ${guard}`).bind(id),
      ...d.sov_lines.map((l) =>
        c.env.DB
          .prepare(`INSERT INTO sov_lines (subcontract_id, ${SOV_COLS}) SELECT ?1, ?2,?3,?4,?5,?6,?7,?8 WHERE ${guard}`)
          .bind(id, l.position, l.item_number, l.description, l.qty, l.unit, l.unit_price_cents, l.extended_cents),
      ),
    ]);
    if ((res[0].meta.changes ?? 0) === 0) {
      const row = await c.env.DB.prepare("SELECT status FROM subcontracts WHERE id = ?1").bind(id).first();
      return row ? c.json({ error: "not_draft" }, 409) : c.json({ error: "not_found" }, 404);
    }
    return c.json({ ok: true, id, subtotal_cents: d.subtotal_cents });
  });

  // POST /api/subcontracts/drafts/:id/generate — the draft→queued transition. Server-side:
  //   1. recompute the SOV subtotal from the STORED lines (never the request body);
  //   2. assert it equals the row's contract_price AND the client's displayed contract_price —
  //      mismatch is a hard 409 (never sign numbers the server disagrees with);
  //   3. fail closed on an unresolvable governing-law state (the render's governing_law.resolve
  //      raises otherwise — fail early beats a queued row that can never render);
  //   4. allocate revision = MAX(revision)+1 within the (job_no, site_phase, supersede_seq) family
  //      and build the D7 sc_number;
  //   5. HMAC-sign "sub:v1"\n<sc_id>\n<sc_number>\n<canonical_json> (HMAC_PAYLOAD_SECRET);
  //   6. flip to 'queued' — allocation + subtotal rewrite + signature + audit in ONE batch, guarded
  //      WHERE status='draft' AND draft_version=?; the UNIQUE family index is the race backstop.
  app.post("/api/subcontracts/drafts/:id/generate", gates.requireSession, gates.requireCapability(CAP_SUB), async (c) => {
    const id = parseIdParam(c.req.param("id"));
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    let body: Record<string, unknown>;
    try {
      body = (await c.req.json()) as Record<string, unknown>;
    } catch {
      return c.json({ error: "bad_request" }, 400);
    }
    if (!isPlainObject(body)) return c.json({ error: "bad_request" }, 400);
    // The client's displayed contract price — the anti-skew assert (replaces PO's subtotal/tax/total).
    if (!isCents(body.contract_price_cents)) return c.json({ error: "invalid_contract_price" }, 400);

    const sub = await c.env.DB
      .prepare("SELECT * FROM subcontracts WHERE id = ?1 AND status = 'draft'")
      .bind(id)
      .first<SubcontractRow & { status: string; draft_version: number; change_order_of: number | null; co_seq: number | null }>();
    if (!sub) return c.json({ error: "not_found" }, 404);
    const lines = await loadSovLines(c.env.DB, id);
    if (lines.length === 0) return c.json({ error: "no_sov_lines" }, 422);

    // Recompute from stored state; the sums-to-price + anti-skew gate, combined.
    const subtotal = computeSubtotal(lines);
    if (typeof subtotal === "string") return c.json({ error: subtotal }, 422);
    if (subtotal !== sub.contract_price_cents || subtotal !== body.contract_price_cents) {
      // Machine-comparable recomputed values ride the refusal so the SPA can re-render — integers
      // only, no PII.
      return c.json({ error: "sov_mismatch", recomputed: { subtotal_cents: subtotal, contract_price_cents: sub.contract_price_cents } }, 409);
    }

    // Governing-law fail-closed: a blank / unresolvable state can never render (governing_law.resolve
    // raises), so refuse here rather than queue a dead row.
    if (!GOVERNING_LAW_STATES.has(sub.governing_law_state)) {
      return c.json({ error: "invalid_governing_law_state" }, 422);
    }
    // Render-required party/scope fields (subcontract_generate _REQUIRED_FIELDS + strict Exhibit tokens):
    // a blank owner_entity / project_name / trade renders to a PERMANENT subcontract_render_failed fence.
    // parseDraftBody stays length-only (a draft may be incomplete); GENERATE is the commit that requires
    // completeness — refuse a blank here (defense-in-depth behind the SPA flag; the API is not bypassable).
    if (!sub.owner_entity?.trim()) return c.json({ error: "missing_owner_entity" }, 422);
    if (!sub.project_name?.trim()) return c.json({ error: "missing_project_name" }, 422);
    if (!sub.trade?.trim()) return c.json({ error: "missing_trade" }, 422);

    // Number allocation forks on document kind (Track D2, the po.ts twin):
    //  - BASE document: revision = family MAX+1, five-segment D7 number.
    //  - CHANGE ORDER (change_order_of set): revision stays NULL (never consumes a family
    //    slot) and the number is `{parent sc_number}-CO{co_seq}` — the parent number rides
    //    INSIDE the signed sc_number, which is how the Mac renders the change-order clause
    //    from signed data (change_order_of/co_seq are store-only, out of sub:v1).
    let revision: number | null;
    let scNumber: string;
    if (sub.change_order_of !== null) {
      // Corrupt-linkage guard: co_seq must exist or the mint below would sign "…-COnull".
      if (sub.co_seq === null) return c.json({ error: "co_linkage_invalid" }, 409);
      const parent = await c.env.DB
        .prepare("SELECT sc_number, status FROM subcontracts WHERE id = ?1")
        .bind(sub.change_order_of)
        .first<{ sc_number: string | null; status: string }>();
      if (!parent?.sc_number) return c.json({ error: "parent_not_generated" }, 409);
      // The rendered notice asserts the parent REMAINS IN FORCE — never sign that against a
      // parent since superseded or canceled ('sent' and 'executed' are the in-force states).
      if (parent.status !== "sent" && parent.status !== "executed") {
        return c.json({ error: "parent_not_in_force" }, 409);
      }
      revision = null;
      scNumber = `${parent.sc_number}-CO${sub.co_seq}`;
    } else {
      // MAX(revision)+1 within the family (allocated tuples only — drafts carry NULL).
      const rev = await c.env.DB
        .prepare(
          "SELECT COALESCE(MAX(revision), -1) + 1 AS rev FROM subcontracts " +
            "WHERE job_no = ?1 AND site_phase = ?2 AND supersede_seq = ?3 AND revision IS NOT NULL",
        )
        .bind(sub.job_no, sub.site_phase, sub.supersede_seq)
        .first<{ rev: number }>();
      revision = rev?.rev ?? 0;
      scNumber = `${sub.job_no}.${sub.site_phase}.${sub.supersede_seq}.${revision}`;
    }

    // Fail closed on a missing HMAC secret — signing with undefined would mint signatures the Mac
    // side can never verify (silent loss).
    if (!c.env.HMAC_PAYLOAD_SECRET) return c.json({ error: "hmac_secret_missing" }, 500);
    const signedSub: SubcontractRow = { ...sub, sc_number: scNumber, revision, subtotal_cents: subtotal };
    const hmac = await hmacHex(c.env.HMAC_PAYLOAD_SECRET, subCanonicalString(id, scNumber, canonicalSubJson(signedSub, lines)));

    const actor = c.get("session").username;
    let res;
    try {
      res = await c.env.DB.batch([
        c.env.DB
          .prepare(
            // Pinned on the draft_version read with the snapshot above: a concurrent draft update
            // landing inside this handler's read→sign→commit window bumps the version, this UPDATE
            // matches 0 rows, and the client gets a clean 'draft_changed' 409 — never a 'queued' row
            // whose HMAC signed a stale snapshot. D1 serializes statements, not whole requests; this
            // is the request-level guard.
            // Parent-in-force clause: makes the CO pre-check ATOMIC with the commit (the po.ts
            // twin; review W5 2026-08-15) — nothing downstream re-checks the parent.
            "UPDATE subcontracts SET revision=?2, sc_number=?3, subtotal_cents=?4, hmac=?5, " +
              "status='queued', updated_at=unixepoch() " +
              "WHERE id=?1 AND status='draft' AND draft_version=?6 " +
              "AND (change_order_of IS NULL OR " +
              "(SELECT s2.status FROM subcontracts s2 WHERE s2.id = subcontracts.change_order_of) IN ('sent','executed'))",
          )
          .bind(id, revision, scNumber, subtotal, hmac, sub.draft_version),
        auditStmtIfChanged(c, actor, "sc_generate", String(id), {
          sc_id: id, sc_number: scNumber, contract_price_cents: sub.contract_price_cents,
        }),
      ]);
    } catch (e) {
      // The UNIQUE family-revision (or sc_number) backstop — a lost allocation race. The draft is
      // untouched; the client simply retries generate and reads a fresh MAX.
      if (isUniqueViolation(e)) return c.json({ error: "sc_number_conflict" }, 409);
      throw e;
    }
    if ((res[0].meta.changes ?? 0) === 0) {
      // Distinguish the three 0-row causes: edited under us (retry-able), left 'draft'
      // entirely, or (CO only) the parent left force inside the read→commit window.
      const now = await c.env.DB
        .prepare("SELECT status, draft_version FROM subcontracts WHERE id = ?1")
        .bind(id)
        .first<{ status: string; draft_version: number }>();
      if (now && now.status === "draft" && now.draft_version !== sub.draft_version) {
        return c.json({ error: "draft_changed" }, 409);
      }
      if (now && now.status === "draft" && sub.change_order_of !== null) {
        // Still a draft at the same version — the only remaining predicate is the parent clause.
        return c.json({ error: "parent_not_in_force" }, 409);
      }
      return c.json({ error: "not_draft" }, 409);
    }
    return c.json({ ok: true, id, sc_number: scNumber, revision, subtotal_cents: subtotal });
  });

  // POST /api/subcontracts/:id/supersede — clone a SENT or EXECUTED subcontract into a new draft:
  // supersede_seq+1, revision/sc_number reset (re-allocated at the clone's own generate), status
  // 'draft', supersedes_sc_id → the source. The OLD subcontract is untouched here — it flips to
  // 'superseded' only when the successor reaches 'sent' (status-sync below). A subcontract's in-force
  // terminal can be 'executed' (wet-signed), so both are supersede sources (decision #4).
  app.post("/api/subcontracts/:id/supersede", gates.requireSession, gates.requireCapability(CAP_SUB), async (c) => {
    const id = parseIdParam(c.req.param("id"));
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    const src = await c.env.DB.prepare("SELECT * FROM subcontracts WHERE id = ?1").bind(id).first<Record<string, unknown>>();
    if (!src) return c.json({ error: "not_found" }, 404);
    // A change-order document can't be superseded — re-issuing a CO is done as the NEXT
    // CO against the base (Track D2, the po.ts twin). Base documents only.
    if ((src.status !== "sent" && src.status !== "executed") || src.change_order_of !== null) {
      return c.json({ error: "not_supersedable" }, 409);
    }
    // Double-submit guard (idempotency): if a live successor draft already exists for this source,
    // don't mint a sibling at the same supersede_seq — surface the existing one. Canceled successors
    // don't block a fresh supersede.
    const dup = await c.env.DB
      .prepare("SELECT id FROM subcontracts WHERE supersedes_sc_id = ?1 AND status != 'canceled'")
      .bind(id)
      .first<{ id: number }>();
    if (dup) return c.json({ error: "supersede_in_progress", existing_id: dup.id }, 409);

    const actor = c.get("session").username;
    const scUuid = crypto.randomUUID();
    // Clone parent + SOV lines + audit in ONE batch (W4); the line clone is a single INSERT..SELECT
    // from the source's lines, subcontract_id resolved via the sc_uuid subquery (constant per
    // statement). AUDIT ORDERING: auditStmtIfChanged's changes() guard reads the IMMEDIATELY PRECEDING
    // statement, so the audit stmt sits directly after the PARENT clone INSERT (1 row iff the clone
    // happened) — placed after the line INSERT it would read the line COUNT and skip the audit row.
    const res = await c.env.DB.batch([
      c.env.DB
        .prepare(
          "INSERT INTO subcontracts (sc_uuid, job_no, site_phase, supersede_seq, job_id, job_name, " +
            "project_name, owner_entity, prime_contractor, site_name, site_address, governing_law_state, " +
            "sub_key, trade, exhibit_a_template_id, exhibit_a_template_version, exhibit_a_work_text, " +
            "scope_summary, price_basis, contract_price_cents, retainage_bp, subtotal_cents, " +
            "start_date, completion_date, terms_profile_id, terms_version, template_family, " +
            "supersedes_sc_id, status, approver_name, approver_title, created_by) " +
            "SELECT ?2, job_no, site_phase, supersede_seq + 1, job_id, job_name, " +
            "project_name, owner_entity, prime_contractor, site_name, site_address, governing_law_state, " +
            "sub_key, trade, exhibit_a_template_id, exhibit_a_template_version, exhibit_a_work_text, " +
            "scope_summary, price_basis, contract_price_cents, retainage_bp, subtotal_cents, " +
            "start_date, completion_date, terms_profile_id, terms_version, template_family, " +
            "?1, 'draft', approver_name, approver_title, ?3 " +
            "FROM subcontracts WHERE id = ?1 AND status IN ('sent','executed')",
        )
        .bind(id, scUuid, actor),
      auditStmtIfChanged(c, actor, "sc_supersede_clone", String(id), { source_sc_id: id, sc_uuid: scUuid }),
      c.env.DB
        .prepare(
          `INSERT INTO sov_lines (subcontract_id, ${SOV_COLS}) ` +
            `SELECT (SELECT id FROM subcontracts WHERE sc_uuid = ?2), ${SOV_COLS} ` +
            "FROM sov_lines WHERE subcontract_id = ?1 " +
            "AND EXISTS (SELECT 1 FROM subcontracts WHERE sc_uuid = ?2)",
        )
        .bind(id, scUuid),
    ]);
    if ((res[0].meta.changes ?? 0) === 0) return c.json({ error: "not_supersedable" }, 409); // lost race
    const clone = await c.env.DB.prepare("SELECT id FROM subcontracts WHERE sc_uuid = ?1").bind(scUuid).first<{ id: number }>();
    return c.json({ ok: true, id: clone?.id ?? null, supersedes_sc_id: id }, 201);
  });

  // POST /api/subcontracts/:id/change-order — clone a SENT or EXECUTED subcontract into a
  // CHANGE-ORDER draft (Track D2, the po.ts twin with the wider in-force source set). The
  // parent stays in force: change_order_of → the source, supersedes_sc_id stays NULL (the
  // supersession flip is structurally inert for a CO), supersede_seq copied VERBATIM,
  // co_seq = MAX+1 over ALL of the parent's COs. At generate the CO mints
  // `{parent sc_number}-CO{co_seq}` instead of allocating a family revision. No CO-of-CO;
  // multiple concurrent CO drafts allowed — the partial unique index is the race backstop.
  app.post("/api/subcontracts/:id/change-order", gates.requireSession, gates.requireCapability(CAP_SUB), async (c) => {
    const id = parseIdParam(c.req.param("id"));
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    const src = await c.env.DB.prepare("SELECT * FROM subcontracts WHERE id = ?1").bind(id).first<Record<string, unknown>>();
    if (!src) return c.json({ error: "not_found" }, 404);
    if ((src.status !== "sent" && src.status !== "executed") || src.change_order_of !== null) {
      return c.json({ error: "not_change_orderable" }, 409);
    }

    const actor = c.get("session").username;
    const scUuid = crypto.randomUUID();
    // Same batch shape + audit ordering as the supersede clone above (the audit stmt reads
    // changes() of the IMMEDIATELY PRECEDING statement — keep it directly after the parent
    // INSERT).
    let res;
    try {
      res = await c.env.DB.batch([
        c.env.DB
          .prepare(
            "INSERT INTO subcontracts (sc_uuid, job_no, site_phase, supersede_seq, job_id, job_name, " +
              "project_name, owner_entity, prime_contractor, site_name, site_address, governing_law_state, " +
              "sub_key, trade, exhibit_a_template_id, exhibit_a_template_version, exhibit_a_work_text, " +
              "scope_summary, price_basis, contract_price_cents, retainage_bp, subtotal_cents, " +
              "start_date, completion_date, terms_profile_id, terms_version, template_family, " +
              "change_order_of, co_seq, status, approver_name, approver_title, created_by) " +
              "SELECT ?2, job_no, site_phase, supersede_seq, job_id, job_name, " +
              "project_name, owner_entity, prime_contractor, site_name, site_address, governing_law_state, " +
              "sub_key, trade, exhibit_a_template_id, exhibit_a_template_version, exhibit_a_work_text, " +
              "scope_summary, price_basis, contract_price_cents, retainage_bp, subtotal_cents, " +
              "start_date, completion_date, terms_profile_id, terms_version, template_family, " +
              "?1, (SELECT COALESCE(MAX(co_seq), 0) + 1 FROM subcontracts WHERE change_order_of = ?1), " +
              "'draft', approver_name, approver_title, ?3 " +
              "FROM subcontracts WHERE id = ?1 AND status IN ('sent','executed') AND change_order_of IS NULL",
          )
          .bind(id, scUuid, actor),
        auditStmtIfChanged(c, actor, "sc_change_order_clone", String(id), { source_sc_id: id, sc_uuid: scUuid }),
        c.env.DB
          .prepare(
            `INSERT INTO sov_lines (subcontract_id, ${SOV_COLS}) ` +
              `SELECT (SELECT id FROM subcontracts WHERE sc_uuid = ?2), ${SOV_COLS} ` +
              "FROM sov_lines WHERE subcontract_id = ?1 " +
              "AND EXISTS (SELECT 1 FROM subcontracts WHERE sc_uuid = ?2)",
          )
          .bind(id, scUuid),
      ]);
    } catch (e) {
      // Lost the co_seq allocation race (partial unique index) — nothing was written; retry.
      if (isUniqueViolation(e)) return c.json({ error: "change_order_conflict" }, 409);
      throw e;
    }
    if ((res[0].meta.changes ?? 0) === 0) return c.json({ error: "not_change_orderable" }, 409); // lost race
    const clone = await c.env.DB
      .prepare("SELECT id, co_seq FROM subcontracts WHERE sc_uuid = ?1")
      .bind(scUuid)
      .first<{ id: number; co_seq: number }>();
    return c.json({ ok: true, id: clone?.id ?? null, change_order_of: id, co_seq: clone?.co_seq ?? null }, 201);
  });

  // POST /api/subcontracts/:id/cancel — off-path terminal, ONLY from draft/queued/pending_review (an
  // approved/sent/executed subcontract is a live commercial instrument — superseding, not cancelling,
  // is its exit; a queued/pending_review cancel is honored Mac-side by the daemon's status read before
  // render/dispatch).
  app.post("/api/subcontracts/:id/cancel", gates.requireSession, gates.requireCapability(CAP_SUB), async (c) => {
    const id = parseIdParam(c.req.param("id"));
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    const actor = c.get("session").username;
    const res = await c.env.DB.batch([
      c.env.DB
        .prepare(
          "UPDATE subcontracts SET status='canceled', updated_at=unixepoch() " +
            "WHERE id=?1 AND status IN ('draft','queued','pending_review')",
        )
        .bind(id),
      auditStmtIfChanged(c, actor, "sc_cancel", String(id), { sc_id: id }),
    ]);
    if ((res[0].meta.changes ?? 0) === 0) {
      const row = await c.env.DB.prepare("SELECT status FROM subcontracts WHERE id = ?1").bind(id).first();
      return row ? c.json({ error: "not_cancelable" }, 409) : c.json({ error: "not_found" }, 404);
    }
    return c.json({ ok: true, id });
  });

  // POST /api/subcontracts/:id/delete — HARD delete of an un-generated DRAFT (the row + its SOV lines).
  // DRAFT-ONLY: a generated/queued/pending_review/approved/sent/executed/superseded/canceled row is a real
  // record (SC number, audit, possibly Box/Smartsheet artifacts) — those exit via cancel/supersede, NEVER a
  // hard delete. Atomic: the sov_lines delete is subquery-scoped to the parent STILL being a draft (no ON
  // DELETE CASCADE on the FK), so a non-draft row leaves BOTH tables untouched — no orphaned lines; the audit
  // lands only if the draft row was actually removed (changes()=1 on the parent delete, W4).
  app.post("/api/subcontracts/:id/delete", gates.requireSession, gates.requireCapability(CAP_SUB), async (c) => {
    const id = parseIdParam(c.req.param("id"));
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    const actor = c.get("session").username;
    const res = await c.env.DB.batch([
      c.env.DB
        .prepare(
          "DELETE FROM sov_lines WHERE subcontract_id IN (SELECT id FROM subcontracts WHERE id=?1 AND status='draft')",
        )
        .bind(id),
      c.env.DB.prepare("DELETE FROM subcontracts WHERE id=?1 AND status='draft'").bind(id),
      auditStmtIfChanged(c, actor, "sc_delete", String(id), { sc_id: id }),
    ]);
    if ((res[1].meta.changes ?? 0) === 0) {
      const row = await c.env.DB.prepare("SELECT status FROM subcontracts WHERE id = ?1").bind(id).first();
      // record_not_deletable, not not_deletable — the photo pool owns that code (see po.ts).
      return row ? c.json({ error: "record_not_deletable" }, 409) : c.json({ error: "not_found" }, 404);
    }
    return c.json({ ok: true, id });
  });

  // ══ Internal surface (requireSubToken — the Mac-side subcontract_poll daemon) ═════

  // GET /api/subcontracts/internal/pending — the queue drain: queued subcontracts + SOV lines + hmac +
  // created_at (the poll derives a STABLE agreement_ymd from created_at's Pacific date, decision #1),
  // oldest-first. The daemon recomputes the sub:v1 canonical HMAC (verify_sub) before trusting a row.
  app.get("/api/subcontracts/internal/pending", gates.requireSubToken, async (c) => {
    const limit = Math.min(Math.max(parseInt(c.req.query("limit") || "50", 10) || 50, 1), SC_PENDING_CAP);
    const { results } = await c.env.DB
      .prepare("SELECT * FROM subcontracts WHERE status = 'queued' ORDER BY updated_at ASC, id ASC LIMIT ?1")
      .bind(limit)
      .all<Record<string, unknown>>();
    const rows = (results ?? []) as Record<string, unknown>[];
    for (const r of rows) r.sov_lines = await loadSovLines(c.env.DB, r.id as number);
    return c.json({ pending: rows });
  });

  // POST /api/subcontracts/internal/mark-filed — the receipt: queued→pending_review + box_file_id
  // (the Subcontract.docx Box file id — the primary signable instrument; the .xlsx + Annex kit are
  // filed alongside in Box and tracked in the Subcontract_Log ledger, not D1). Idempotent: a replay
  // (already pending_review) is a no-op — ok:true, found:false — and the guarded audit writes nothing.
  app.post("/api/subcontracts/internal/mark-filed", gates.requireSubToken, async (c) => {
    let body: Record<string, unknown>;
    try {
      body = (await c.req.json()) as Record<string, unknown>;
    } catch {
      return c.json({ error: "bad_request" }, 400);
    }
    if (!isPlainObject(body)) return c.json({ error: "bad_request" }, 400);
    const scId = typeof body.sc_id === "number" && Number.isSafeInteger(body.sc_id) && body.sc_id > 0 ? body.sc_id : null;
    const boxFileId = typeof body.box_file_id === "string" ? body.box_file_id.slice(0, 200) : null;
    if (scId === null) return c.json({ error: "invalid_sc_id" }, 400);
    const res = await c.env.DB.batch([
      c.env.DB
        .prepare(
          "UPDATE subcontracts SET status='pending_review', box_file_id=?2, updated_at=unixepoch() " +
            "WHERE id=?1 AND status='queued'",
        )
        .bind(scId, boxFileId),
      auditStmtIfChanged(c, SYSTEM_ACTOR, "sc_mark_filed", String(scId), { sc_id: scId, box_file_id: boxFileId }),
    ]);
    return c.json({ ok: true, found: (res[0].meta.changes ?? 0) > 0 });
  });

  // POST /api/subcontracts/internal/status-sync — Mac-side machine outcomes (approved/sent/executed/
  // superseded stamps from F22 approval + the send poller + the operator's Subcontract_Log Status
  // edit). D1 status here is a CACHE of the Mac/Smartsheet authoritative state; the in-WHERE guards
  // exist to prevent REGRESSION (a stale/replayed sync can never move a status backwards), not to
  // re-enforce F22 — the real approval gate is Mac-side. SUPERSESSION FLIP: when a subcontract reaches
  // 'sent' and carries supersedes_sc_id, the superseded predecessor flips in the SAME batch, guarded on
  // the successor actually being 'sent' at execution time and the predecessor still in force (sent OR
  // executed — a predecessor may already be countersigned; decision #4).
  app.post("/api/subcontracts/internal/status-sync", gates.requireSubToken, async (c) => {
    let body: { updates?: unknown };
    try {
      body = await c.req.json();
    } catch {
      return c.json({ error: "bad_request" }, 400);
    }
    if (!isPlainObject(body)) return c.json({ error: "bad_request" }, 400);
    const raw = body.updates;
    if (!Array.isArray(raw)) return c.json({ error: "invalid_updates" }, 400);
    if (raw.length === 0) return c.json({ error: "empty_updates" }, 400);
    if (raw.length > SC_STATUS_SYNC_CAP) return c.json({ error: "too_many_updates" }, 413);

    const statements = [];
    const touched: string[] = [];
    for (const u of raw) {
      if (!isPlainObject(u)) return c.json({ error: "invalid_update" }, 400);
      const scId = typeof u.sc_id === "number" && Number.isSafeInteger(u.sc_id) && u.sc_id > 0 ? u.sc_id : null;
      const status = typeof u.status === "string" && SYNCABLE_STATUSES.has(u.status) ? u.status : null;
      if (scId === null || status === null) return c.json({ error: "invalid_update" }, 400);
      if (status === "approved") {
        statements.push(
          c.env.DB
            .prepare("UPDATE subcontracts SET status='approved', updated_at=unixepoch() WHERE id=?1 AND status='pending_review'")
            .bind(scId),
          auditStmtIfChanged(c, SYSTEM_ACTOR, "sc_status_approved", String(scId), { sc_id: scId }),
        );
      } else if (status === "sent") {
        statements.push(
          c.env.DB
            .prepare("UPDATE subcontracts SET status='sent', updated_at=unixepoch() WHERE id=?1 AND status='approved'")
            .bind(scId),
          auditStmtIfChanged(c, SYSTEM_ACTOR, "sc_status_sent", String(scId), { sc_id: scId }),
          // The supersession flip — same batch (W4): the predecessor goes 'superseded' IFF this
          // subcontract is NOW 'sent' in D1 (re-checked at execution time, after the UPDATE above) and
          // the predecessor is still an in-force 'sent'/'executed' document.
          c.env.DB
            .prepare(
              "UPDATE subcontracts SET status='superseded', updated_at=unixepoch() " +
                "WHERE id = (SELECT supersedes_sc_id FROM subcontracts WHERE id = ?1) " +
                "AND status IN ('sent','executed') " +
                "AND (SELECT status FROM subcontracts WHERE id = ?1) = 'sent'",
            )
            .bind(scId),
          auditStmtIfChanged(c, SYSTEM_ACTOR, "sc_superseded_flip", String(scId), { successor_sc_id: scId }),
        );
      } else if (status === "executed") {
        // The wet-signature countersign terminal (decision #4; corpus '_FE'). No supersession side-effect.
        statements.push(
          c.env.DB
            .prepare("UPDATE subcontracts SET status='executed', updated_at=unixepoch() WHERE id=?1 AND status='sent'")
            .bind(scId),
          auditStmtIfChanged(c, SYSTEM_ACTOR, "sc_status_executed", String(scId), { sc_id: scId }),
        );
      } else {
        // Manual 'superseded' stamp — an operator-driven Mac-side supersession record; a subcontract's
        // in-force document may already be 'executed', so both are retirable (decision #4).
        statements.push(
          c.env.DB
            .prepare("UPDATE subcontracts SET status='superseded', updated_at=unixepoch() WHERE id=?1 AND status IN ('sent','executed')")
            .bind(scId),
          auditStmtIfChanged(c, SYSTEM_ACTOR, "sc_status_superseded", String(scId), { sc_id: scId }),
        );
      }
      touched.push(`${scId}:${status}`);
    }
    await c.env.DB.batch(statements);
    return c.json({ ok: true, updated: touched.length });
  });

  // POST /api/subcontracts/internal/subcontractors/sync — the Smartsheet→D1 full-replace down-sync
  // (D4/§51). UPSERT every supplied row EXCEPT dirty ones: the conflict-UPDATE carries
  // `WHERE subcontractors.sync_state != 'pending'` — THE dirty-row fence — so an un-mirrored portal edit
  // is never clobbered. Refuses an empty payload (a Smartsheet read-miss must never wipe the cache);
  // NEVER deletes — a sheet-retired subcontractor arrives with active=0. Watermarks are untouched (the
  // UP-sync's bookkeeping).
  app.post("/api/subcontracts/internal/subcontractors/sync", gates.requireSubToken, async (c) => {
    let body: { subcontractors?: unknown };
    try {
      body = await c.req.json();
    } catch {
      return c.json({ error: "bad_request" }, 400);
    }
    if (!isPlainObject(body)) return c.json({ error: "bad_request" }, 400);
    const raw = body.subcontractors;
    if (!Array.isArray(raw)) return c.json({ error: "invalid_subcontractors" }, 400);
    if (raw.length === 0) return c.json({ error: "empty_subcontractors" }, 400);
    if (raw.length > MAX_SYNC_ROWS) return c.json({ error: "too_many_subcontractors" }, 413);

    // Validate + normalize every row up front; reject the WHOLE batch on any bad row (a partial sync
    // would silently desync the cache).
    const rows: (SubcontractorFields & { sub_key: string })[] = [];
    const seen = new Set<string>();
    for (const r of raw) {
      if (!isPlainObject(r)) return c.json({ error: "invalid_row" }, 400);
      const sub_key = str(r.sub_key);
      if (!SUB_KEY_RE.test(sub_key)) return c.json({ error: "invalid_row" }, 400);
      if (seen.has(sub_key)) return c.json({ error: "duplicate_sub_key" }, 400);
      seen.add(sub_key);
      const f = parseSubcontractorFields(r);
      if (typeof f === "string") return c.json({ error: "invalid_row", field: f }, 400);
      rows.push({ sub_key, ...f });
    }

    const statements = rows.map((v) =>
      c.env.DB
        .prepare(
          "INSERT INTO subcontractors (sub_key, sub_name, address, contact_name, contact_email, " +
            "contact_phone, state, trades, default_terms_profile, msa_reference, coi_reference, " +
            "license_number, active, notes, origin, sync_state, mirror_version, mirrored_version) " +
            "VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,'smartsheet','synced',0,0) " +
            "ON CONFLICT(sub_key) DO UPDATE SET " +
            "sub_name=excluded.sub_name, address=excluded.address, contact_name=excluded.contact_name, " +
            "contact_email=excluded.contact_email, contact_phone=excluded.contact_phone, state=excluded.state, " +
            "trades=excluded.trades, default_terms_profile=excluded.default_terms_profile, " +
            "msa_reference=excluded.msa_reference, coi_reference=excluded.coi_reference, " +
            "license_number=excluded.license_number, active=excluded.active, notes=excluded.notes, " +
            "origin='smartsheet', updated_at=unixepoch() " +
            "WHERE subcontractors.sync_state != 'pending'", // ← THE dirty-row fence
        )
        .bind(
          v.sub_key, v.sub_name, v.address, v.contact_name, v.contact_email, v.contact_phone,
          v.state, v.trades, v.default_terms_profile, v.msa_reference, v.coi_reference,
          v.license_number, v.active, v.notes,
        ),
    );
    statements.push(
      c.env.DB
        .prepare("INSERT INTO audit_log (actor_username, action, target_username, detail) VALUES (?1,?2,?3,?4)")
        .bind(SYSTEM_ACTOR, "sc_subcontractors_sync", "", JSON.stringify({ supplied: rows.length })),
    );
    const results = await c.env.DB.batch(statements);
    // Per-row meta.changes: 1 = inserted/updated, 0 = fenced (dirty row skipped).
    let upserted = 0;
    for (let i = 0; i < rows.length; i++) if ((results[i]?.meta?.changes ?? 0) > 0) upserted++;
    return c.json({ ok: true, upserted, skipped_dirty: rows.length - upserted });
  });

  // GET /api/subcontracts/internal/subcontractors/pending — the up-sync read: portal-edited (dirty)
  // rows + the version vector. The daemon bridge-key find-or-creates the ITS_Subcontractors row by
  // sub_key, then commits via mark-mirrored below.
  app.get("/api/subcontracts/internal/subcontractors/pending", gates.requireSubToken, async (c) => {
    const { results } = await c.env.DB
      .prepare(
        "SELECT sub_key, sub_name, address, contact_name, contact_email, contact_phone, " +
          "state, trades, default_terms_profile, msa_reference, coi_reference, license_number, " +
          "active, notes, origin, mirror_version, mirrored_version " +
          "FROM subcontractors WHERE sync_state = 'pending' ORDER BY mirror_version ASC, sub_key ASC LIMIT ?1",
      )
      .bind(SUB_PENDING_CAP)
      .all<Record<string, unknown>>();
    const subcontractors = (results ?? []).map((v) => ({ ...v, trades: parseJsonArray(v.trades) }));
    return c.json({ subcontractors });
  });

  // POST /api/subcontracts/internal/subcontractors/mark-mirrored — the up-sync commit point:
  // pending→synced + mirrored_version=mirror_version, ONLY IF mirror_version is UNCHANGED since the
  // daemon's pending read (the watermark guard, bound in-WHERE) — a portal edit racing the mirror bumps
  // mirror_version, the guard fails, the row STAYS pending and re-up-syncs next cycle. Idempotent.
  app.post("/api/subcontracts/internal/subcontractors/mark-mirrored", gates.requireSubToken, async (c) => {
    let body: { updates?: unknown };
    try {
      body = await c.req.json();
    } catch {
      return c.json({ error: "bad_request" }, 400);
    }
    if (!isPlainObject(body)) return c.json({ error: "bad_request" }, 400);
    const raw = body.updates;
    if (!Array.isArray(raw)) return c.json({ error: "invalid_updates" }, 400);
    if (raw.length === 0) return c.json({ error: "empty_updates" }, 400);
    if (raw.length > SUB_PENDING_CAP) return c.json({ error: "too_many_updates" }, 413);

    const updates: { sub_key: string; mirrored_version: number }[] = [];
    for (const u of raw) {
      if (!isPlainObject(u)) return c.json({ error: "invalid_update" }, 400);
      const sub_key = str(u.sub_key);
      const version = typeof u.mirrored_version === "number" && Number.isSafeInteger(u.mirrored_version) && u.mirrored_version >= 1
        ? u.mirrored_version
        : null;
      if (!SUB_KEY_RE.test(sub_key) || version === null) return c.json({ error: "invalid_update" }, 400);
      updates.push({ sub_key, mirrored_version: version });
    }
    const statements = updates.map((u) =>
      c.env.DB
        .prepare(
          "UPDATE subcontractors SET mirrored_version=?2, sync_state='synced', updated_at=unixepoch() " +
            "WHERE sub_key=?1 AND sync_state='pending' AND mirror_version=?2",
        )
        .bind(u.sub_key, u.mirrored_version),
    );
    statements.push(
      c.env.DB
        .prepare("INSERT INTO audit_log (actor_username, action, target_username, detail) VALUES (?1,?2,?3,?4)")
        .bind(SYSTEM_ACTOR, "sc_subcontractors_mark_mirrored", "", JSON.stringify({
          count: updates.length,
          keys: updates.slice(0, 50).map((u) => u.sub_key),
        })),
    );
    const results = await c.env.DB.batch(statements);
    let flipped = 0;
    for (let i = 0; i < updates.length; i++) if ((results[i]?.meta?.changes ?? 0) > 0) flipped++;
    return c.json({ ok: true, flipped, stale: updates.length - flipped });
  });
}
