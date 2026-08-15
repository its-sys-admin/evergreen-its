/// <reference types="vite/client" />
import type { MiddlewareHandler } from "hono";
import type { FieldopsApp } from "./fieldops_gates";
import { auditStmt, auditStmtIfChanged, isUniqueViolation } from "./audit";
import { hmacHex } from "./hmac";
import { CO_SCOPE_PO_DELTA } from "./wire-types";
import { MAX_ADDRESS } from "./constants";
// S2b wiring — the S3 terms manifest + versioned purchaser/tax config, imported at
// BUILD time from po_materials/ (the same files the Mac renderer reads at render time).
import termsManifest from "../../po_materials/terms/manifest.json";
import purchaserConfig from "../../po_materials/config/purchaser.json";
import taxConfig from "../../po_materials/config/tax.json";
import deliveryContactsConfig from "../../po_materials/config/delivery_contacts.json";

// ─────────────────────────────────────────────────────────────────────────────
// PO workstream S2 (Aug-7 delivery program WS1) — worker/po.ts
//
// The Worker half of the Purchase-Order pipeline: browser routes (session +
// cap.po.manage) for the vendor cache + PO drafts/generate/supersede/cancel, and
// internal routes under the NEW requirePoToken bearer tier (PORTAL_PO_API_TOKEN /
// Keychain ITS_PORTAL_PO_TOKEN) that the Mac-side po_poll daemon (S4) consumes.
//
// Invariants:
//   - Invariant 1 (External Send Gate): SEND-FREE — this module performs zero
//     external transmission and has zero AI step. It validates, computes, signs,
//     and queues in D1; the Mac daemon pulls, renders, files; the SEPARATE
//     po_send.py transmits only after F22-verified human approval.
//   - Invariant 2 (Adversarial Input): PO drafts arrive from authenticated office
//     admins but are still client-supplied data — every body is shape-guarded +
//     bounded, ALL money is recomputed server-side in integer cents (a client
//     whose displayed totals disagree is REJECTED), all SQL is bound, every
//     mutation batches atomically with its audit row (W4), and the queued payload
//     is HMAC-signed under the NEW domain prefix "po:v1" (same secret as
//     submissions, different domain — a PO signature can never replay as a
//     submission and vice versa).
//   - §51 bidirectional rider (D4): po_vendors is a CACHE of the ITS_Vendors SoR.
//     Down-sync is full-replace WITH the dirty-row fence (a sync_state='pending'
//     portal edit is never clobbered) and refuses an empty payload; up-sync is
//     watermarked (mark-mirrored flips pending→synced only if mirror_version is
//     unchanged). NEVER deletes — deactivate only.
//
// D7 numbering: po_number = `${job_no}.${site_phase}.${supersede_seq}.${revision}`,
// revision allocated at generate as MAX(revision)+1 within the family, with the
// UNIQUE index idx_po_family_revision (migration 0043) as the race backstop.
//
// S2b wiring: GET /api/po/terms + GET /api/po/config serve the S3 terms manifest +
// purchaser/tax config (build-time JSON imports above) — one source shared with the
// Mac-side renderer; the generate-time totals assert catches deploy skew.
// ─────────────────────────────────────────────────────────────────────────────

export type PoGates = {
  requireSession: MiddlewareHandler<{ Bindings: import("./types").Env; Variables: import("./types").Vars }>;
  requireCapability: (cap: string) => MiddlewareHandler<{ Bindings: import("./types").Env; Variables: import("./types").Vars }>;
  /** Bearer gate for /api/po/internal/* — the Mac-side po_poll daemon's OWN token tier
   *  (PORTAL_PO_API_TOKEN), privilege-separated from the portal_poll / admin / fieldops
   *  tokens. Built in index.ts next to its siblings (same fail-closed constant-time shape). */
  requirePoToken: MiddlewareHandler<{ Bindings: import("./types").Env; Variables: import("./types").Vars }>;
};

const CAP_PO = "cap.po.manage";
const PO_HMAC_DOMAIN = "po:v1";

// ── Tax table — sourced from the S3 versioned config at build time (D8; basis
//    points). 'auto' mode FAILS CLOSED on a state not in this table — a silent 0%
//    on an unknown state would understate tax on a legal document.
const TAX_RATE_BP: Record<string, number> = taxConfig.rates_bp;

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
const TERMS_RAW = import.meta.glob<string>("../../po_materials/terms/*.md", {
  query: "?raw",
  import: "default",
  eager: true,
});

// Port of po_materials/terms.py::_strip_header_comment — drop a leading <!-- ... --> provenance
// block (maintainer docs that must never reach a rendered PO or the editor textarea). Only a
// comment at the very top is stripped; a malformed unterminated one is served raw (this editor
// pre-fill is a convenience — the Mac renderer's strict loader is the authority and hard-raises).
function stripTermsHeader(text: string): string {
  const s = text.replace(/^\s+/, "");
  if (!s.startsWith("<!--")) return text;
  const end = s.indexOf("-->");
  if (end === -1) return text;
  return s.slice(end + 3).replace(/^\n+/, "");
}

// ── Bounds (Invariant 2) ────────────────────────────────────────────────────────
const MAX_KEY = 64;
const MAX_NAME = 256;
const MAX_SHORT = 64;
const MAX_PHONE = 40;
const MAX_EMAIL = 320;
const MAX_NOTES = 2000;
const MAX_SOW = 8000;
const MAX_INSTRUCTIONS = 4000;
const MAX_TERMS_TEXT = 2000;
const MAX_CATEGORIES = 20;
const MAX_LINES = 100;
const MAX_LINE_TEXT = 512;
const MAX_QTY = 1_000_000_000;
const MAX_COUNT = 1_000_000_000; // watts / panels / pallets
const MAX_MONEY_CENTS = 1_000_000_000_000; // $10B — generous ceiling on any single money value
const MAX_PPW_MICROCENTS = 1_000_000_000_000;
const MAX_SYNC_ROWS = 5000;
const PO_PENDING_CAP = 50;
const PO_STATUS_SYNC_CAP = 200;
const VENDOR_PENDING_CAP = 200;
const LIST_CAP = 200;
// The catalog is a small controlled vocabulary (0019 seeds 36 active types); a single bounded
// read serves the whole active set to the line-item picker — no keyset pagination needed.
const MATERIALS_CAP = 500;

const VENDOR_KEY_RE = /^VEN-\d{6}$/;
const JOB_NO_RE = /^\d{4}\.\d{3}$/; // the Evergreen '{YYYY.NNN}' job number (D7)
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/; // matches fieldops_job_write / active_jobs
const STATE_RE = /^[A-Z]{2}$/;

const TAX_MODES = new Set(["auto", "exempt", "included", "override"]);
const LINE_VARIANTS = new Set(["default", "lump_sum", "per_watt"]);
const PO_STATUSES = new Set(["draft", "queued", "pending_review", "approved", "sent", "superseded", "canceled"]);
// The statuses the Mac-side status-sync may stamp (draft/queued/pending_review/canceled are
// Worker-owned transitions; the daemon reports only the Mac-side machine's outcomes).
const SYNCABLE_STATUSES = new Set(["approved", "sent", "superseded"]);

// System actor for the token-gated internal routes (no session) — the fieldops
// "system:fieldops_sync" convention.
const SYSTEM_ACTOR = "system:po_poll";

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

// ── Vendor field validation (create + update share it) ─────────────────────────
interface VendorFields {
  vendor_name: string;
  address: string;
  contact_name: string;
  contact_email: string;
  contact_phone: string;
  region: string;
  supply_categories: string; // stored JSON text
  default_terms_profile: string;
  gtc_reference: string;
  active: number;
  notes: string;
}

function parseVendorFields(body: Record<string, unknown>): VendorFields | string {
  const vendor_name = str(body.vendor_name);
  if (vendor_name.length < 1 || vendor_name.length > MAX_NAME) return "invalid_vendor_name";
  const address = str(body.address);
  if (address.length > MAX_ADDRESS) return "invalid_address";
  const contact_name = str(body.contact_name);
  if (contact_name.length > MAX_NAME) return "invalid_contact_name";
  const contact_email = str(body.contact_email);
  if (contact_email.length > MAX_EMAIL || (contact_email && !EMAIL_RE.test(contact_email))) return "invalid_contact_email";
  const contact_phone = str(body.contact_phone);
  if (contact_phone.length > MAX_PHONE) return "invalid_contact_phone";
  const region = str(body.region);
  if (region.length > MAX_SHORT) return "invalid_region";
  let categories: string[] = [];
  if (body.supply_categories !== undefined && body.supply_categories !== null) {
    if (!Array.isArray(body.supply_categories) || body.supply_categories.length > MAX_CATEGORIES) return "invalid_supply_categories";
    for (const s of body.supply_categories) {
      if (typeof s !== "string" || s.length < 1 || s.length > MAX_SHORT) return "invalid_supply_categories";
    }
    categories = body.supply_categories;
  }
  const default_terms_profile = str(body.default_terms_profile);
  if (default_terms_profile.length > MAX_SHORT) return "invalid_default_terms_profile";
  const gtc_reference = str(body.gtc_reference);
  if (gtc_reference.length > MAX_NAME) return "invalid_gtc_reference";
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
    vendor_name, address, contact_name, contact_email, contact_phone, region,
    supply_categories: JSON.stringify(categories),
    default_terms_profile, gtc_reference, active, notes,
  };
}

// ── Line-item + money validation/computation (D8) ───────────────────────────────
export interface PoLine {
  position: number;
  part_number: string;
  description: string;
  qty: number;
  unit: string;
  unit_cost_cents: number | null;
  extended_cents: number;
  watts: number | null;
  panels: number | null;
  pallets: number | null;
  price_per_watt_microcents: number | null;
}

function optCount(v: unknown): number | null | "bad" {
  if (v === undefined || v === null) return null;
  if (typeof v !== "number" || !Number.isSafeInteger(v) || v < 0 || v > MAX_COUNT) return "bad";
  return v;
}

/** Server-side extended-cents for one line — INTEGER MATH ONLY on money:
 *  per-watt lines (watts + price_per_watt_microcents present) use
 *  round(watts × ppw_microcents / 1e6); every other line uses round(qty × unit_cost_cents).
 *  Exported so the vitest suite pins the exact rounding the HMAC covers. */
export function lineExtendedCents(l: {
  qty: number; unit_cost_cents: number | null; watts: number | null; price_per_watt_microcents: number | null;
}): number {
  if (l.watts !== null && l.price_per_watt_microcents !== null) {
    return Math.round((l.watts * l.price_per_watt_microcents) / 1_000_000);
  }
  return Math.round(l.qty * (l.unit_cost_cents ?? 0));
}

/** Parse + bound the client line array; positions are SERVER-assigned (1-based array order)
 *  and extended_cents is SERVER-computed — the client's opinion of either is ignored. */
function parseLines(raw: unknown, variant: string): PoLine[] | string {
  if (!Array.isArray(raw) || raw.length < 1 || raw.length > MAX_LINES) return "invalid_line_items";
  const out: PoLine[] = [];
  for (let i = 0; i < raw.length; i++) {
    const r = raw[i];
    if (!isPlainObject(r)) return "invalid_line_items";
    const part_number = str(r.part_number);
    if (part_number.length > MAX_SHORT) return "invalid_part_number";
    const description = str(r.description);
    if (description.length < 1 || description.length > MAX_LINE_TEXT) return "invalid_description";
    const unit = str(r.unit);
    if (unit.length > 32) return "invalid_unit";
    // qty: finite, bounded, ≤3 decimal places (normalized) so the canonical-JSON HMAC
    // serialization is bit-stable across the JS/Python recompute (shortest-roundtrip
    // doubles agree on both sides for ≤3dp decimals).
    const qtyRaw = r.qty;
    if (typeof qtyRaw !== "number" || !Number.isFinite(qtyRaw) || qtyRaw < 0 || qtyRaw > MAX_QTY) return "invalid_qty";
    const qty = Math.round(qtyRaw * 1000) / 1000;
    let unit_cost_cents: number | null = null;
    if (r.unit_cost_cents !== undefined && r.unit_cost_cents !== null) {
      if (!isCents(r.unit_cost_cents)) return "invalid_unit_cost_cents";
      unit_cost_cents = r.unit_cost_cents;
    }
    const watts = optCount(r.watts);
    const panels = optCount(r.panels);
    const pallets = optCount(r.pallets);
    if (watts === "bad" || panels === "bad" || pallets === "bad") return "invalid_per_watt_fields";
    let ppw: number | null = null;
    if (r.price_per_watt_microcents !== undefined && r.price_per_watt_microcents !== null) {
      if (typeof r.price_per_watt_microcents !== "number" || !Number.isSafeInteger(r.price_per_watt_microcents) ||
          r.price_per_watt_microcents < 0 || r.price_per_watt_microcents > MAX_PPW_MICROCENTS) {
        return "invalid_price_per_watt";
      }
      ppw = r.price_per_watt_microcents;
    }
    // A per-watt VARIANT PO must price every line per-watt (the rendered columns are
    // watts/panels/pallets/$-per-W); a default/lump_sum line must carry a unit cost.
    if (variant === "per_watt") {
      if (watts === null || ppw === null) return "per_watt_fields_required";
    } else if (unit_cost_cents === null && !(watts !== null && ppw !== null)) {
      return "unit_cost_required";
    }
    const line: PoLine = {
      position: i + 1, part_number, description, qty, unit, unit_cost_cents,
      extended_cents: 0, watts, panels, pallets, price_per_watt_microcents: ppw,
    };
    line.extended_cents = lineExtendedCents(line);
    if (line.extended_cents > MAX_MONEY_CENTS) return "line_total_overflow";
    out.push(line);
  }
  return out;
}

export interface PoTotals {
  subtotal_cents: number;
  tax_rate_bp: number; // the RESOLVED rate (auto → table value; exempt/included → 0)
  tax_cents: number;
  total_cents: number;
}

/** Recompute subtotal/tax/total from server-validated lines (D8). Exported so the vitest
 *  suite pins the math the totals-assert + HMAC cover. Returns an error code string on an
 *  unresolvable tax basis — 'auto' FAILS CLOSED on a state missing from TAX_RATE_BP. */
export function computeTotals(
  lines: PoLine[],
  taxMode: string,
  taxRateBpOverride: number,
  shippingCents: number,
  shipToState: string,
): PoTotals | string {
  let subtotal = 0;
  for (const l of lines) subtotal += l.extended_cents;
  if (subtotal > MAX_MONEY_CENTS) return "subtotal_overflow";
  let rate = 0;
  if (taxMode === "auto") {
    const t = TAX_RATE_BP[shipToState];
    if (t === undefined) return "unknown_tax_state";
    rate = t;
  } else if (taxMode === "override") {
    rate = taxRateBpOverride;
  } // exempt / included → 0 (included = tax already inside the line prices; no added line)
  const tax = Math.round((subtotal * rate) / 10_000);
  const total = subtotal + tax + shippingCents;
  if (total > MAX_MONEY_CENTS) return "total_overflow";
  return { subtotal_cents: subtotal, tax_rate_bp: rate, tax_cents: tax, total_cents: total };
}

// ── Draft body validation ───────────────────────────────────────────────────────
interface DraftFields {
  job_no: string;
  site_phase: number;
  job_id: string;
  job_name: string;
  ship_to_name: string;
  ship_to_address: string;
  ship_to_city: string;
  ship_to_state: string;
  ship_to_zip: string;
  delivery_contact_name: string;
  delivery_contact_phone: string;
  delivery_contact_email: string;
  sow_text: string;
  delivery_instructions: string;
  payment_terms_text: string;
  terms_profile_id: string;
  terms_version: string;
  tax_mode: string;
  shipping_cents: number;
  line_column_variant: string;
  approver_name: string;
  approver_title: string;
  vendor_key: string;
  /** ADR-0004 red-team #4 — estimate-import PROVENANCE (which po_estimates row this draft
   *  was imported from). STORE-ONLY: consumed by the create route's idempotency guard and
   *  the stored column; it must NEVER enter canonicalPoJson / the po:v1 HMAC string (the
   *  Python-side recompute would break). null = an ordinary hand-built draft. */
  estimate_id: number | null;
  lines: PoLine[];
  totals: PoTotals;
}

function parseDraftBody(body: Record<string, unknown>): DraftFields | string {
  const vendor_key = str(body.vendor_key);
  if (!VENDOR_KEY_RE.test(vendor_key)) return "invalid_vendor_key";
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
  const ship_to_name = str(body.ship_to_name);
  const ship_to_address = str(body.ship_to_address);
  const ship_to_city = str(body.ship_to_city);
  const ship_to_zip = str(body.ship_to_zip);
  if (ship_to_name.length > MAX_NAME || ship_to_address.length > MAX_ADDRESS ||
      ship_to_city.length > MAX_NAME || ship_to_zip.length > 16) {
    return "invalid_ship_to";
  }
  const ship_to_state = str(body.ship_to_state).toUpperCase();
  if (ship_to_state && !STATE_RE.test(ship_to_state)) return "invalid_ship_to_state";
  const delivery_contact_name = str(body.delivery_contact_name);
  if (delivery_contact_name.length > MAX_NAME) return "invalid_delivery_contact";
  const delivery_contact_phone = str(body.delivery_contact_phone);
  if (delivery_contact_phone.length > MAX_PHONE) return "invalid_delivery_contact";
  const delivery_contact_email = str(body.delivery_contact_email);
  if (delivery_contact_email.length > MAX_EMAIL || (delivery_contact_email && !EMAIL_RE.test(delivery_contact_email))) {
    return "invalid_delivery_contact";
  }
  const sow_text = str(body.sow_text);
  if (sow_text.length > MAX_SOW) return "invalid_sow_text";
  const delivery_instructions = str(body.delivery_instructions);
  if (delivery_instructions.length > MAX_INSTRUCTIONS) return "invalid_delivery_instructions";
  const payment_terms_text = str(body.payment_terms_text);
  if (payment_terms_text.length > MAX_TERMS_TEXT) return "invalid_payment_terms_text";
  const terms_profile_id = str(body.terms_profile_id);
  if (terms_profile_id.length > MAX_SHORT) return "invalid_terms_profile";
  const terms_version = str(body.terms_version);
  if (terms_version.length > 32) return "invalid_terms_version";
  const tax_mode = str(body.tax_mode) || "auto";
  if (!TAX_MODES.has(tax_mode)) return "invalid_tax_mode";
  let taxRateBpOverride = 0;
  if (tax_mode === "override") {
    const bp = body.tax_rate_bp;
    if (typeof bp !== "number" || !Number.isSafeInteger(bp) || bp < 0 || bp > 10_000) return "invalid_tax_rate_bp";
    taxRateBpOverride = bp;
  }
  let shipping_cents = 0;
  if (body.shipping_cents !== undefined && body.shipping_cents !== null) {
    if (!isCents(body.shipping_cents)) return "invalid_shipping_cents";
    shipping_cents = body.shipping_cents;
  }
  const line_column_variant = str(body.line_column_variant) || "default";
  if (!LINE_VARIANTS.has(line_column_variant)) return "invalid_line_column_variant";
  const approver_name = str(body.approver_name);
  const approver_title = str(body.approver_title);
  if (approver_name.length > MAX_NAME || approver_title.length > MAX_NAME) return "invalid_approver";
  // Optional estimate-import provenance (ADR-0004): a positive integer or absent.
  let estimate_id: number | null = null;
  if (body.estimate_id !== undefined && body.estimate_id !== null) {
    if (typeof body.estimate_id !== "number" || !Number.isSafeInteger(body.estimate_id) || body.estimate_id <= 0) {
      return "invalid_estimate_id";
    }
    estimate_id = body.estimate_id;
  }
  // 'auto' needs a resolvable tax basis at DRAFT time already — failing early beats a
  // draft that can never generate.
  if (tax_mode === "auto" && !ship_to_state) return "invalid_ship_to_state";

  const lines = parseLines(body.line_items, line_column_variant);
  if (typeof lines === "string") return lines;
  const totals = computeTotals(lines, tax_mode, taxRateBpOverride, shipping_cents, ship_to_state);
  if (typeof totals === "string") return totals;

  return {
    job_no, site_phase, job_id, job_name,
    ship_to_name, ship_to_address, ship_to_city, ship_to_state, ship_to_zip,
    delivery_contact_name, delivery_contact_phone, delivery_contact_email,
    sow_text, delivery_instructions, payment_terms_text,
    terms_profile_id, terms_version,
    tax_mode, shipping_cents, line_column_variant,
    approver_name, approver_title, vendor_key, estimate_id, lines, totals,
  };
}

// ── HMAC canonical payload (domain 'po:v1') ─────────────────────────────────────
// The Mac-side po_poll daemon (S4, shared/portal_hmac.py PO_DOMAIN) recomputes this
// byte-for-byte before rendering/filing. ORDER + SEPARATOR are load-bearing:
//   "po:v1" \n po_id \n po_number \n canonical_payload_json
// canonical_payload_json is JSON.stringify of the FIXED-KEY-ORDER object below (insertion
// order preserved; the Python side mirrors with json.dumps(..., separators=(",", ":")) over
// the same key order). All money/count values are integers; qty is a ≤3dp double whose
// shortest-roundtrip serialization agrees across JS/Python.
export interface PoRow {
  id: number;
  po_number: string | null;
  job_no: string;
  site_phase: number;
  supersede_seq: number;
  revision: number | null;
  vendor_key: string;
  job_id: string;
  job_name: string;
  ship_to_name: string;
  ship_to_address: string;
  ship_to_city: string;
  ship_to_state: string;
  ship_to_zip: string;
  delivery_contact_name: string;
  delivery_contact_phone: string;
  delivery_contact_email: string;
  sow_text: string;
  delivery_instructions: string;
  payment_terms_text: string;
  terms_profile_id: string;
  terms_version: string;
  subtotal_cents: number;
  tax_mode: string;
  tax_rate_bp: number;
  tax_cents: number;
  shipping_cents: number;
  total_cents: number;
  line_column_variant: string;
  supersedes_po_id: number | null;
  approver_name: string;
  approver_title: string;
}

export function canonicalPoJson(po: PoRow, lines: PoLine[]): string {
  return JSON.stringify({
    po_number: po.po_number,
    job_no: po.job_no,
    site_phase: po.site_phase,
    supersede_seq: po.supersede_seq,
    revision: po.revision,
    vendor_key: po.vendor_key,
    job_id: po.job_id,
    job_name: po.job_name,
    ship_to_name: po.ship_to_name,
    ship_to_address: po.ship_to_address,
    ship_to_city: po.ship_to_city,
    ship_to_state: po.ship_to_state,
    ship_to_zip: po.ship_to_zip,
    delivery_contact_name: po.delivery_contact_name,
    delivery_contact_phone: po.delivery_contact_phone,
    delivery_contact_email: po.delivery_contact_email,
    sow_text: po.sow_text,
    delivery_instructions: po.delivery_instructions,
    payment_terms_text: po.payment_terms_text,
    terms_profile_id: po.terms_profile_id,
    terms_version: po.terms_version,
    subtotal_cents: po.subtotal_cents,
    tax_mode: po.tax_mode,
    tax_rate_bp: po.tax_rate_bp,
    tax_cents: po.tax_cents,
    shipping_cents: po.shipping_cents,
    total_cents: po.total_cents,
    line_column_variant: po.line_column_variant,
    supersedes_po_id: po.supersedes_po_id,
    approver_name: po.approver_name,
    approver_title: po.approver_title,
    line_items: lines.map((l) => ({
      position: l.position,
      part_number: l.part_number,
      description: l.description,
      qty: l.qty,
      unit: l.unit,
      unit_cost_cents: l.unit_cost_cents,
      extended_cents: l.extended_cents,
      watts: l.watts,
      panels: l.panels,
      pallets: l.pallets,
      price_per_watt_microcents: l.price_per_watt_microcents,
    })),
  });
}

export function poCanonicalString(poId: number, poNumber: string, canonicalJson: string): string {
  return [PO_HMAC_DOMAIN, String(poId), poNumber, canonicalJson].join("\n");
}

// The line columns every read path selects (one definition, N readers — no drift).
const LINE_COLS =
  "position, part_number, description, qty, unit, unit_cost_cents, extended_cents, " +
  "watts, panels, pallets, price_per_watt_microcents";

async function loadLines(db: D1Database, poId: number): Promise<PoLine[]> {
  const { results } = await db
    .prepare(`SELECT ${LINE_COLS} FROM po_line_items WHERE po_id = ?1 ORDER BY position ASC`)
    .bind(poId)
    .all<PoLine>();
  return (results ?? []) as PoLine[];
}

// ── Route registration ──────────────────────────────────────────────────────────
export function registerPoRoutes(app: FieldopsApp, gates: PoGates): void {
  // ══ Browser surface (session + cap.po.manage) ══════════════════════════════════

  // GET /api/po/terms — the terms-library picker feed (S2b wiring; S3 manifest,
  // build-time import). Serves the SPA a curated view: profile id/kind/label +
  // the current version + its tokens (library) or the render line (attach) —
  // never the raw manifest (hash pins and file names are renderer implementation
  // detail, not picker data).
  app.get("/api/po/terms", gates.requireSession, gates.requireCapability(CAP_PO), (c) => {
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

  // GET /api/po/terms/:profile_id/text — the CURRENT version's clause BODY (header-stripped), for
  // the config editor's "edit text" pre-fill so the operator edits from the live wording, then saves
  // it as a NEW version (add_version). Library profiles only — attach profiles render a fixed
  // render_line and have no versioned text. Read-only: cap.po.manage, no mutation, no audit row.
  app.get(
    "/api/po/terms/:profile_id/text",
    gates.requireSession,
    gates.requireCapability(CAP_PO),
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

  // GET /api/po/terms/:profile_id/versions — the version list for a library profile, so the config
  // editor's "make current" picker shows every version + its legal_review status + which is current.
  // CURATED: version id + legal_review only — file names / sha256 stay off the wire (renderer
  // implementation detail, like the sibling /api/po/terms). cap.po.manage, read-only, no audit.
  app.get(
    "/api/po/terms/:profile_id/versions",
    gates.requireSession,
    gates.requireCapability(CAP_PO),
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

  // GET /api/po/config — the versioned purchaser identity (D5) + tax table (D8) +
  // delivery-contact suggestion list (Feature C) for the builder UI (entity display,
  // invoice-routing cc chips, tax-state badge, delivery-contact <datalist>).
  // Explicit key picks — the JSON files carry maintainer comment fields that don't
  // belong on the wire.
  app.get("/api/po/config", gates.requireSession, gates.requireCapability(CAP_PO), (c) =>
    c.json({
      purchaser: {
        entity: purchaserConfig.entity,
        address_lines: purchaserConfig.address_lines,
        phone: purchaserConfig.phone,
        invoice_routing: purchaserConfig.invoice_routing,
      },
      tax: {
        rates_bp: taxConfig.rates_bp,
        state_names: taxConfig.state_names,
      },
      // Suggestions for the builder's delivery-contact name <datalist> — free text always
      // stays accepted (the list never gates a draft; §50-edited, bundled at build time).
      delivery_contacts: deliveryContactsConfig.contacts,
    }),
  );

  // GET /api/po/materials — the line-item catalog picker feed. A THIN, read-only view of the
  // SAME material_catalog TYPE table the field-ops Materials Catalog admin manages (migration
  // 0019) — deliberately NOT a new po-specific catalog table. Gated cap.po.manage (the PO
  // builder's own capability) so a PO admin reads the pick-list WITHOUT being granted the
  // field-ops cap.materials.receive that /api/fieldops/materials requires — the PO builder's
  // data reads all sit under one cap. material_catalog is a TYPE vocabulary (manufacturer /
  // model / specs, NO price), so the picker only populates a line's IDENTITY (part_number +
  // description); qty/unit/unit_cost stay per-PO operator entry. Read-only → no mutation, no W4
  // audit row; bound params only; active types only, optional ?category= filter, hard-capped.
  app.get("/api/po/materials", gates.requireSession, gates.requireCapability(CAP_PO), async (c) => {
    const category = str(c.req.query("category")).slice(0, MAX_SHORT);
    const { results } = await c.env.DB
      .prepare(
        "SELECT id, model_id, manufacturer, category, key_specs FROM material_catalog " +
          "WHERE active = 1 AND (?1 = '' OR category = ?1) " +
          "ORDER BY model_id ASC, id ASC LIMIT ?2",
      )
      .bind(category, MATERIALS_CAP)
      .all<Record<string, unknown>>();
    return c.json({ materials: results ?? [] });
  });

  // GET /api/po/jobs/:job_id/ship-to — the builder's ship-to + delivery auto-fill feed (S6
  // follow-up: closes the "ship-to ADDRESS block stays manual" deviation that PoBuilderPage's
  // header comment documents). It reads the SAME routing SoR row the internal-token tier serves
  // over GET /api/internal/fieldops/pending-jobs (jobs.address + stakeholder_*), but under the
  // BROWSER session + cap.po.manage gate — so the office can auto-fill without ever exposing the
  // internal token. READ-ONLY, bound SQL, single-PK lookup, no mutation. These are Evergreen's
  // OWN job/site addresses (the purchaser's routing data, not third-party PII); the block returned
  // is exactly the fields the builder's ship-to step already collects, no more. The routing SoR
  // carries a single free-text `address` line (NOT a structured city/state/zip — those columns
  // exist only on purchase_orders, 0043), so city/state/zip ride back empty and stay operator-
  // editable; a future structured-address SoR just fills them. Auto-fill is a CONVENIENCE only:
  // every field is editable in the UI, and a 404 / absent field silently leaves it blank.
  app.get("/api/po/jobs/:job_id/ship-to", gates.requireSession, gates.requireCapability(CAP_PO), async (c) => {
    const jobId = c.req.param("job_id");
    const row = await c.env.DB
      .prepare(
        "SELECT job_id, project_name, address, stakeholder_name, stakeholder_phone, stakeholder_email, " +
          "job_no, site_phase, address_city, address_state, address_zip " +
          "FROM jobs WHERE job_id = ?1",
      )
      .bind(jobId)
      .first<{
        job_id: string;
        project_name: string;
        address: string;
        stakeholder_name: string;
        stakeholder_phone: string;
        stakeholder_email: string;
        job_no: string;
        site_phase: number;
        address_city: string;
        address_state: string;
        address_zip: string;
      }>();
    if (!row) return c.json({ error: "not_found" }, 404);
    // job_no + site_phase: the STORED Evergreen identifier (0057 + 0064) first; the
    // name-prefix parse stays as the legacy fallback for jobs that predate the structured
    // fields.
    //
    // The fallback regex now captures the OPTIONAL third segment and is anchored at BOTH
    // ends of the number (`(?![\d.])` — end-of-number, not end-of-string, since the name
    // continues: "2026.384.1 Coker Solar"). Before 0064 it was `/^(\d{4}\.\d{3})/`, open at
    // the tail: against that same name it MATCHED and returned the truncated "2026.384",
    // silently handing the builder a wrong-but-plausible number for a DIFFERENT site of the
    // same project — the one surface in this fan-out that corrupted rather than refused.
    const jobNoMatch = /^(\d{4}\.\d{3})(?:\.(\d+))?(?![\d.])/.exec((row.project_name ?? "").trim());
    const fallbackJobNo = jobNoMatch ? jobNoMatch[1] : "";
    // Bounded by the SAME ceiling parseDraftBody enforces on a typed site_phase (0..9999): an
    // out-of-range segment in a project NAME resolves to 0 (no site) rather than pre-filling a
    // value the very next request would reject as invalid_site_phase.
    const parsedSite = jobNoMatch && jobNoMatch[2] !== undefined ? parseInt(jobNoMatch[2], 10) : 0;
    const fallbackSitePhase = Number.isSafeInteger(parsedSite) && parsedSite >= 0 && parsedSite <= 9999 ? parsedSite : 0;
    // Both parts come from the SAME source — stored or parsed, never one of each. Mixing a
    // stored job_no with a name-parsed site (or vice versa) would compose a document number
    // for a site the operator never named.
    const hasStoredJobNo = (row.job_no ?? "") !== "";
    return c.json({
      job_id: row.job_id,
      job_no: hasStoredJobNo ? row.job_no : fallbackJobNo,
      // 0064 — the identifier's third segment, feeding the builder's Site/phase auto-fill.
      site_phase: hasStoredJobNo ? (row.site_phase ?? 0) : fallbackSitePhase,
      ship_to_name: row.project_name ?? "",
      ship_to_address: row.address ?? "",
      // 0057: the structured address block (street stays in `address`); '' when the job
      // predates the fields — the builder inputs stay operator-editable either way.
      ship_to_city: row.address_city ?? "",
      ship_to_state: row.address_state ?? "",
      ship_to_zip: row.address_zip ?? "",
      delivery_contact_name: row.stakeholder_name ?? "",
      delivery_contact_phone: row.stakeholder_phone ?? "",
      delivery_contact_email: row.stakeholder_email ?? "",
    });
  });

  // GET /api/po/vendors — the vendor picker/management read. Active-only by default;
  // ?include_inactive=1 widens (the management list shows retired vendors greyed).
  app.get("/api/po/vendors", gates.requireSession, gates.requireCapability(CAP_PO), async (c) => {
    const includeInactive = c.req.query("include_inactive") === "1";
    const { results } = await c.env.DB
      .prepare(
        "SELECT vendor_key, vendor_name, address, contact_name, contact_email, contact_phone, " +
          "region, supply_categories, default_terms_profile, gtc_reference, active, notes, " +
          "origin, sync_state, mirror_version " +
          "FROM po_vendors WHERE (?1 = 1 OR active = 1) ORDER BY vendor_name ASC",
      )
      .bind(includeInactive ? 1 : 0)
      .all<Record<string, unknown>>();
    const vendors = (results ?? []).map((v) => ({ ...v, supply_categories: parseJsonArray(v.supply_categories) }));
    return c.json({ vendors });
  });

  // POST /api/po/vendors — portal vendor create (§51 rider, D4). Allocates the next
  // VEN-###### atomically: the single UPDATE..RETURNING both self-heals past the max
  // suffix already present (a key that arrived via down-sync) AND increments — D1
  // serializes writes, so two concurrent creates get distinct keys; the PK is the
  // backstop (isUniqueViolation → 409). origin='portal' + sync_state='pending' +
  // mirror_version=1 makes the new row immediately dirty for the up-sync.
  app.post("/api/po/vendors", gates.requireSession, gates.requireCapability(CAP_PO), async (c) => {
    let body: Record<string, unknown>;
    try {
      body = (await c.req.json()) as Record<string, unknown>;
    } catch {
      return c.json({ error: "bad_request" }, 400);
    }
    if (!isPlainObject(body)) return c.json({ error: "bad_request" }, 400);
    const f = parseVendorFields(body);
    if (typeof f === "string") return c.json({ error: f }, 400);

    let counter: { last_value: number } | null;
    try {
      counter = await c.env.DB
        .prepare(
          "UPDATE po_vendor_counter SET last_value = MAX(last_value, COALESCE((" +
            "SELECT MAX(CAST(substr(vendor_key, 5) AS INTEGER)) FROM po_vendors WHERE vendor_key LIKE 'VEN-______'" +
            "), 0)) + 1 WHERE id = 1 RETURNING last_value",
        )
        .first<{ last_value: number }>();
    } catch {
      counter = null; // table absent (0042 not applied) → fail closed, same as a missing seed row
    }
    if (!counter) return c.json({ error: "counter_unavailable" }, 500);
    const vendorKey = `VEN-${String(counter.last_value).padStart(6, "0")}`;

    const actor = c.get("session").username;
    try {
      await c.env.DB.batch([
        c.env.DB
          .prepare(
            "INSERT INTO po_vendors (vendor_key, vendor_name, address, contact_name, contact_email, " +
              "contact_phone, region, supply_categories, default_terms_profile, gtc_reference, active, notes, " +
              "origin, sync_state, mirror_version, mirrored_version) " +
              "VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,'portal','pending',1,0)",
          )
          .bind(
            vendorKey, f.vendor_name, f.address, f.contact_name, f.contact_email, f.contact_phone,
            f.region, f.supply_categories, f.default_terms_profile, f.gtc_reference, f.active, f.notes,
          ),
        auditStmt(c, actor, "po_vendor_create", vendorKey, { vendor_key: vendorKey, vendor_name: f.vendor_name }),
      ]);
    } catch (e) {
      if (isUniqueViolation(e)) return c.json({ error: "vendor_exists" }, 409);
      throw e;
    }
    return c.json({ ok: true, vendor_key: vendorKey }, 201);
  });

  // POST /api/po/vendors/:vendor_key/update — full-field portal edit (deactivation rides
  // `active: 0` — NEVER a delete, D4). Re-dirties the row for the up-sync: origin='portal'
  // (authorship of the current cached version), sync_state='pending', mirror_version+1 —
  // the bump is what invalidates a racing mark-mirrored's watermark.
  app.post("/api/po/vendors/:vendor_key/update", gates.requireSession, gates.requireCapability(CAP_PO), async (c) => {
    const vendorKey = c.req.param("vendor_key");
    if (!VENDOR_KEY_RE.test(vendorKey)) return c.json({ error: "invalid_vendor_key" }, 400);
    let body: Record<string, unknown>;
    try {
      body = (await c.req.json()) as Record<string, unknown>;
    } catch {
      return c.json({ error: "bad_request" }, 400);
    }
    if (!isPlainObject(body)) return c.json({ error: "bad_request" }, 400);
    const f = parseVendorFields(body);
    if (typeof f === "string") return c.json({ error: f }, 400);

    const actor = c.get("session").username;
    const res = await c.env.DB.batch([
      c.env.DB
        .prepare(
          "UPDATE po_vendors SET vendor_name=?2, address=?3, contact_name=?4, contact_email=?5, " +
            "contact_phone=?6, region=?7, supply_categories=?8, default_terms_profile=?9, " +
            "gtc_reference=?10, active=?11, notes=?12, " +
            "origin='portal', sync_state='pending', mirror_version=mirror_version+1, updated_at=unixepoch() " +
            "WHERE vendor_key=?1",
        )
        .bind(
          vendorKey, f.vendor_name, f.address, f.contact_name, f.contact_email, f.contact_phone,
          f.region, f.supply_categories, f.default_terms_profile, f.gtc_reference, f.active, f.notes,
        ),
      auditStmtIfChanged(c, actor, "po_vendor_update", vendorKey, { vendor_key: vendorKey, active: f.active }),
    ]);
    if ((res[0].meta.changes ?? 0) === 0) return c.json({ error: "not_found" }, 404);
    return c.json({ ok: true, vendor_key: vendorKey });
  });

  // GET /api/po/pos?status=&limit= — the tracker list (drafts + queued + the rest).
  // Status validated against the fixed vocabulary (+ 'all', the default).
  app.get("/api/po/pos", gates.requireSession, gates.requireCapability(CAP_PO), async (c) => {
    const q = c.req.query();
    const status = PO_STATUSES.has(q.status ?? "") ? (q.status as string) : "all";
    const limit = Math.min(Math.max(parseInt(q.limit || "50", 10) || 50, 1), LIST_CAP);
    const { results } = await c.env.DB
      .prepare(
        "SELECT id, po_number, job_no, site_phase, supersede_seq, revision, vendor_key, job_id, job_name, " +
          "status, total_cents, supersedes_po_id, change_order_of, co_seq, box_file_id, created_by, created_at, updated_at " +
          "FROM purchase_orders WHERE (?1 = 'all' OR status = ?1) ORDER BY updated_at DESC, id DESC LIMIT ?2",
      )
      .bind(status, limit)
      .all<Record<string, unknown>>();
    return c.json({ pos: results ?? [] });
  });

  // GET /api/po/pos/:id — one PO + its line items (any status; the SPA detail/preview read).
  app.get("/api/po/pos/:id", gates.requireSession, gates.requireCapability(CAP_PO), async (c) => {
    const id = parseIdParam(c.req.param("id"));
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    const po = await c.env.DB.prepare("SELECT * FROM purchase_orders WHERE id = ?1").bind(id).first<Record<string, unknown>>();
    if (!po) return c.json({ error: "not_found" }, 404);
    const line_items = await loadLines(c.env.DB, id);
    return c.json({ po, line_items });
  });

  // POST /api/po/drafts — create a draft. The vendor must exist AND be active (422 —
  // shape is fine, the reference isn't). All money server-computed (D8); the stored
  // extended/subtotal/tax/total are the SERVER's numbers, whatever the client displayed —
  // the hard assert against the client's numbers happens at generate.
  app.post("/api/po/drafts", gates.requireSession, gates.requireCapability(CAP_PO), async (c) => {
    let body: Record<string, unknown>;
    try {
      body = (await c.req.json()) as Record<string, unknown>;
    } catch {
      return c.json({ error: "bad_request" }, 400);
    }
    if (!isPlainObject(body)) return c.json({ error: "bad_request" }, 400);
    const d = parseDraftBody(body);
    if (typeof d === "string") return c.json({ error: d }, 400);

    const vendor = await c.env.DB
      .prepare("SELECT vendor_key FROM po_vendors WHERE vendor_key = ?1 AND active = 1")
      .bind(d.vendor_key)
      .first();
    if (!vendor) return c.json({ error: "unknown_vendor" }, 422);

    const actor = c.get("session").username;
    const poUuid = crypto.randomUUID();
    // Parent + audit + all lines in ONE batch (W4). The line INSERTs resolve po_id via a
    // scalar subquery on po_uuid — constant during each statement (the parent row landed in
    // statement 1); last_insert_rowid() would MOVE per inserted row and is deliberately avoided.
    //
    // ESTIMATE-IMPORT IDEMPOTENCY (ADR-0004 red-team #4): when the draft carries an
    // estimate_id, the parent INSERT is guarded IN-WHERE on no non-canceled PO already
    // referencing that estimate — evaluated at execution time (D1 serializes statements), so
    // a double-import race writes exactly one draft. A refused insert writes NOTHING: the
    // audit is changes()-gated directly after the parent, and every line INSERT is guarded
    // on the parent row existing. estimate_id is STORE-ONLY provenance — it is deliberately
    // NOT in canonicalPoJson / the po:v1 HMAC (the Python recompute would break).
    const stmts = [
      c.env.DB
        .prepare(
          "INSERT INTO purchase_orders (po_uuid, job_no, site_phase, supersede_seq, job_id, job_name, " +
            "ship_to_name, ship_to_address, ship_to_city, ship_to_state, ship_to_zip, " +
            "delivery_contact_name, delivery_contact_phone, delivery_contact_email, " +
            "sow_text, delivery_instructions, payment_terms_text, terms_profile_id, terms_version, " +
            "subtotal_cents, tax_mode, tax_rate_bp, tax_cents, shipping_cents, total_cents, " +
            "line_column_variant, status, approver_name, approver_title, vendor_key, created_by, estimate_id) " +
            "SELECT ?1,?2,?3,0,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17,?18,?19,?20,?21,?22,?23,?24,?25,'draft',?26,?27,?28,?29,?30 " +
            "WHERE (?30 IS NULL OR NOT EXISTS " +
            "(SELECT 1 FROM purchase_orders WHERE estimate_id = ?30 AND status != 'canceled')) " +
            "RETURNING id",
        )
        .bind(
          poUuid, d.job_no, d.site_phase, d.job_id, d.job_name,
          d.ship_to_name, d.ship_to_address, d.ship_to_city, d.ship_to_state, d.ship_to_zip,
          d.delivery_contact_name, d.delivery_contact_phone, d.delivery_contact_email,
          d.sow_text, d.delivery_instructions, d.payment_terms_text, d.terms_profile_id, d.terms_version,
          d.totals.subtotal_cents, d.tax_mode, d.totals.tax_rate_bp, d.totals.tax_cents, d.shipping_cents,
          d.totals.total_cents, d.line_column_variant, d.approver_name, d.approver_title, d.vendor_key, actor,
          d.estimate_id,
        ),
      // Directly after the parent (changes() reads the immediately preceding statement) —
      // a guard-refused create writes no lying audit row.
      auditStmtIfChanged(c, actor, "po_draft_create", poUuid, {
        po_uuid: poUuid, job_no: d.job_no, site_phase: d.site_phase, vendor_key: d.vendor_key,
        total_cents: d.totals.total_cents, estimate_id: d.estimate_id,
      }),
      ...d.lines.map((l) =>
        c.env.DB
          .prepare(
            `INSERT INTO po_line_items (po_id, ${LINE_COLS}) ` +
              "SELECT (SELECT id FROM purchase_orders WHERE po_uuid = ?1), ?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12 " +
              "WHERE EXISTS (SELECT 1 FROM purchase_orders WHERE po_uuid = ?1)",
          )
          .bind(
            poUuid, l.position, l.part_number, l.description, l.qty, l.unit, l.unit_cost_cents,
            l.extended_cents, l.watts, l.panels, l.pallets, l.price_per_watt_microcents,
          ),
      ),
    ];
    const res = await c.env.DB.batch(stmts);
    const id = (res[0].results?.[0] as { id: number } | undefined)?.id ?? null;
    if (id === null) {
      // The only in-WHERE guard on this INSERT is the estimate-import idempotency check.
      return c.json({ error: "estimate_already_imported" }, 409);
    }
    return c.json({ ok: true, id, totals: d.totals }, 201);
  });

  // POST /api/po/drafts/:id/update — full-replace edit, DRAFT-ONLY (guarded in-WHERE; the
  // line DELETE/INSERTs are each guarded on the live status so a lost race writes nothing).
  app.post("/api/po/drafts/:id/update", gates.requireSession, gates.requireCapability(CAP_PO), async (c) => {
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

    const vendor = await c.env.DB
      .prepare("SELECT vendor_key FROM po_vendors WHERE vendor_key = ?1 AND active = 1")
      .bind(d.vendor_key)
      .first();
    if (!vendor) return c.json({ error: "unknown_vendor" }, 422);

    // Track D2 identity lock: a change-order draft amends ONE parent contract — its
    // counterparty and job identity are the parent's, and its number will be minted off the
    // parent's. The full-replace body always carries these fields, so a repoint (new vendor,
    // different job) would silently produce a CO numbered against a contract it no longer
    // amends. Everything else (lines, totals, sow, terms, ship-to) stays freely editable —
    // that IS the change order.
    const cur = await c.env.DB
      .prepare("SELECT change_order_of, vendor_key, job_no, site_phase, job_id FROM purchase_orders WHERE id = ?1")
      .bind(id)
      .first<{ change_order_of: number | null; vendor_key: string; job_no: string; site_phase: number; job_id: string }>();
    if (
      cur && cur.change_order_of !== null &&
      (d.vendor_key !== cur.vendor_key || d.job_no !== cur.job_no || d.site_phase !== cur.site_phase || d.job_id !== cur.job_id)
    ) {
      return c.json({ error: "co_identity_locked" }, 409);
    }

    const actor = c.get("session").username;
    const guard = "(SELECT status FROM purchase_orders WHERE id = ?1) = 'draft'";
    const res = await c.env.DB.batch([
      c.env.DB
        .prepare(
          "UPDATE purchase_orders SET job_no=?2, site_phase=?3, job_id=?4, job_name=?5, " +
            "ship_to_name=?6, ship_to_address=?7, ship_to_city=?8, ship_to_state=?9, ship_to_zip=?10, " +
            "delivery_contact_name=?11, delivery_contact_phone=?12, delivery_contact_email=?13, " +
            "sow_text=?14, delivery_instructions=?15, payment_terms_text=?16, terms_profile_id=?17, terms_version=?18, " +
            "subtotal_cents=?19, tax_mode=?20, tax_rate_bp=?21, tax_cents=?22, shipping_cents=?23, total_cents=?24, " +
            "line_column_variant=?25, approver_name=?26, approver_title=?27, vendor_key=?28, updated_at=unixepoch(), " +
            // draft_version covers the WHOLE draft snapshot (this route rewrites parent AND
            // lines together) — generate() pins its status flip on the version it read.
            "draft_version=draft_version+1 " +
            "WHERE id=?1 AND status='draft'",
        )
        .bind(
          id, d.job_no, d.site_phase, d.job_id, d.job_name,
          d.ship_to_name, d.ship_to_address, d.ship_to_city, d.ship_to_state, d.ship_to_zip,
          d.delivery_contact_name, d.delivery_contact_phone, d.delivery_contact_email,
          d.sow_text, d.delivery_instructions, d.payment_terms_text, d.terms_profile_id, d.terms_version,
          d.totals.subtotal_cents, d.tax_mode, d.totals.tax_rate_bp, d.totals.tax_cents, d.shipping_cents,
          d.totals.total_cents, d.line_column_variant, d.approver_name, d.approver_title, d.vendor_key,
        ),
      auditStmtIfChanged(c, actor, "po_draft_update", String(id), { po_id: id, total_cents: d.totals.total_cents }),
      // Full-replace the line set — status-guarded so a non-draft row's lines are untouched
      // even if this batch raced the generate (status is not modified above, so the subquery
      // reflects the true row state).
      c.env.DB.prepare(`DELETE FROM po_line_items WHERE po_id = ?1 AND ${guard}`).bind(id),
      ...d.lines.map((l) =>
        c.env.DB
          .prepare(`INSERT INTO po_line_items (po_id, ${LINE_COLS}) SELECT ?1, ?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12 WHERE ${guard}`)
          .bind(
            id, l.position, l.part_number, l.description, l.qty, l.unit, l.unit_cost_cents,
            l.extended_cents, l.watts, l.panels, l.pallets, l.price_per_watt_microcents,
          ),
      ),
    ]);
    if ((res[0].meta.changes ?? 0) === 0) {
      const row = await c.env.DB.prepare("SELECT status FROM purchase_orders WHERE id = ?1").bind(id).first();
      return row ? c.json({ error: "not_draft" }, 409) : c.json({ error: "not_found" }, 404);
    }
    return c.json({ ok: true, id, totals: d.totals });
  });

  // POST /api/po/drafts/:id/generate — the draft→queued transition. Server-side:
  //   1. recompute ALL money from the STORED lines (never the request body);
  //   2. assert the client's displayed totals match — mismatch is a hard 409 (the client
  //      showed the office admin numbers the server disagrees with; never sign those);
  //   3. allocate revision = MAX(revision)+1 within the (job_no, site_phase, supersede_seq)
  //      family and build the D7 po_number;
  //   4. HMAC-sign "po:v1"\n<po_id>\n<po_number>\n<canonical_json> (HMAC_PAYLOAD_SECRET);
  //   5. flip to 'queued' — allocation + money rewrite + signature + audit in ONE batch,
  //      guarded WHERE status='draft'; the UNIQUE family index is the race backstop (two
  //      generates that read the same MAX both build revision N; the loser hits UNIQUE → 409).
  app.post("/api/po/drafts/:id/generate", gates.requireSession, gates.requireCapability(CAP_PO), async (c) => {
    const id = parseIdParam(c.req.param("id"));
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    let body: Record<string, unknown>;
    try {
      body = (await c.req.json()) as Record<string, unknown>;
    } catch {
      return c.json({ error: "bad_request" }, 400);
    }
    if (!isPlainObject(body)) return c.json({ error: "bad_request" }, 400);
    // The client's displayed totals — the anti-skew assert (D8). All three required.
    if (!isCents(body.subtotal_cents) || !isCents(body.tax_cents) || !isCents(body.total_cents)) {
      return c.json({ error: "invalid_totals" }, 400);
    }

    const po = await c.env.DB
      .prepare("SELECT * FROM purchase_orders WHERE id = ?1 AND status = 'draft'")
      .bind(id)
      .first<PoRow & { tax_mode: string; shipping_cents: number; status: string; draft_version: number; change_order_of: number | null; co_seq: number | null }>();
    if (!po) return c.json({ error: "not_found" }, 404);
    const lines = await loadLines(c.env.DB, id);
    if (lines.length === 0) return c.json({ error: "no_line_items" }, 422);

    // Recompute from stored state. For 'override' the stored tax_rate_bp IS the override
    // (resolved at draft save); auto re-resolves from the table (fail-closed on unknown state).
    const totals = computeTotals(lines, po.tax_mode, po.tax_rate_bp, po.shipping_cents, po.ship_to_state);
    if (typeof totals === "string") return c.json({ error: totals }, 422);
    if (
      totals.subtotal_cents !== body.subtotal_cents ||
      totals.tax_cents !== body.tax_cents ||
      totals.total_cents !== body.total_cents
    ) {
      // Machine-comparable recomputed values ride the refusal so the SPA can re-render —
      // integers only, no PII.
      return c.json({ error: "totals_mismatch", recomputed: totals }, 409);
    }
    // Render-required: po_generate resolve_terms(get_profile) FENCES permanently on a blank terms profile
    // (reachable when the vendor has no default). parseDraftBody stays length-only (a draft may be
    // incomplete); GENERATE requires it — refuse a blank here (defense-in-depth behind the SPA flag).
    if (!po.terms_profile_id.trim()) return c.json({ error: "missing_terms_profile" }, 422);

    // Number allocation forks on document kind (Track D2):
    //  - BASE document: revision = family MAX+1, five-segment D7 number.
    //  - CHANGE ORDER (change_order_of set): revision stays NULL — a CO never consumes
    //    a family slot (the MAX subquery's `revision IS NOT NULL` excludes it) — and the
    //    number is `{parent po_number}-CO{co_seq}`. The parent number is read live at
    //    generate: the parent was 'sent' at clone time, so it MUST have one; a NULL here
    //    means the linkage is corrupt and we refuse rather than mint an orphan number.
    //    The parent number rides INSIDE the signed po_number, which is how the Mac
    //    renders the change-order clause from signed data (store-only columns stay out
    //    of the canonical — see parseDraftBody's estimate_id note).
    let revision: number | null;
    let poNumber: string;
    if (po.change_order_of !== null) {
      // Corrupt-linkage guard: co_seq must exist or the mint below would sign "…-COnull".
      if (po.co_seq === null) return c.json({ error: "co_linkage_invalid" }, 409);
      const parent = await c.env.DB
        .prepare("SELECT po_number, status FROM purchase_orders WHERE id = ?1")
        .bind(po.change_order_of)
        .first<{ po_number: string | null; status: string }>();
      if (!parent?.po_number) return c.json({ error: "parent_not_generated" }, 409);
      // The rendered clause asserts the parent REMAINS IN FORCE — never sign that against a
      // parent since superseded or canceled. A CO drafted before its parent was replaced is
      // refused here and re-issued against the successor (design-completion review, 2026-08-15).
      if (parent.status !== "sent") return c.json({ error: "parent_not_in_force" }, 409);
      revision = null;
      poNumber = `${parent.po_number}-CO${po.co_seq}`;
    } else {
      // MAX(revision)+1 within the family (allocated tuples only — drafts carry NULL).
      const rev = await c.env.DB
        .prepare(
          "SELECT COALESCE(MAX(revision), -1) + 1 AS rev FROM purchase_orders " +
            "WHERE job_no = ?1 AND site_phase = ?2 AND supersede_seq = ?3 AND revision IS NOT NULL",
        )
        .bind(po.job_no, po.site_phase, po.supersede_seq)
        .first<{ rev: number }>();
      revision = rev?.rev ?? 0;
      poNumber = `${po.job_no}.${po.site_phase}.${po.supersede_seq}.${revision}`;
    }

    // Fail closed on a missing HMAC secret — signing with undefined would mint signatures
    // the Mac side can never verify (silent loss), the exact failure buildSubmissionInsert
    // documents as the caller's job to prevent.
    if (!c.env.HMAC_PAYLOAD_SECRET) return c.json({ error: "hmac_secret_missing" }, 500);
    const signedPo: PoRow = { ...po, po_number: poNumber, revision, ...totals };
    const hmac = await hmacHex(c.env.HMAC_PAYLOAD_SECRET, poCanonicalString(id, poNumber, canonicalPoJson(signedPo, lines)));

    const actor = c.get("session").username;
    let res;
    try {
      res = await c.env.DB.batch([
        c.env.DB
          .prepare(
            // Pinned on the draft_version read with the snapshot above: a concurrent draft
            // update landing inside this handler's read→sign→commit window bumps the version,
            // this UPDATE matches 0 rows, and the client gets a clean 'draft_changed' 409 —
            // never a 'queued' row whose HMAC signed a stale snapshot (review finding W5/W8).
            // D1 serializes statements, not whole requests; this is the request-level guard.
            // The parent-in-force clause makes the CO pre-check ATOMIC with the commit (the
            // status-sync flip's correlated-subquery pattern): a parent superseded between the
            // read above and this statement can no longer let a signed "remains in force"
            // clause through (review W5, 2026-08-15). Nothing downstream re-checks — the Mac
            // renders the clause from the signed number unconditionally.
            "UPDATE purchase_orders SET revision=?2, po_number=?3, subtotal_cents=?4, tax_rate_bp=?5, " +
              "tax_cents=?6, total_cents=?7, hmac=?8, status='queued', updated_at=unixepoch() " +
              "WHERE id=?1 AND status='draft' AND draft_version=?9 " +
              "AND (change_order_of IS NULL OR " +
              "(SELECT p2.status FROM purchase_orders p2 WHERE p2.id = purchase_orders.change_order_of) = 'sent')",
          )
          .bind(id, revision, poNumber, totals.subtotal_cents, totals.tax_rate_bp, totals.tax_cents, totals.total_cents, hmac, po.draft_version),
        auditStmtIfChanged(c, actor, "po_generate", String(id), {
          po_id: id, po_number: poNumber, total_cents: totals.total_cents,
        }),
      ]);
    } catch (e) {
      // The UNIQUE family-revision (or po_number) backstop — a lost allocation race. The
      // draft is untouched; the client simply retries generate and reads a fresh MAX.
      if (isUniqueViolation(e)) return c.json({ error: "po_number_conflict" }, 409);
      throw e;
    }
    if ((res[0].meta.changes ?? 0) === 0) {
      // Distinguish the three 0-row causes: the draft was edited under us (retry-able —
      // refetch, re-verify totals, regenerate), the row left 'draft' entirely, or (CO only)
      // the parent left force inside the read→commit window.
      const now = await c.env.DB
        .prepare("SELECT status, draft_version FROM purchase_orders WHERE id = ?1")
        .bind(id)
        .first<{ status: string; draft_version: number }>();
      if (now && now.status === "draft" && now.draft_version !== po.draft_version) {
        return c.json({ error: "draft_changed" }, 409);
      }
      if (now && now.status === "draft" && po.change_order_of !== null) {
        // Still a draft at the same version — the only remaining predicate is the parent clause.
        return c.json({ error: "parent_not_in_force" }, 409);
      }
      return c.json({ error: "not_draft" }, 409);
    }
    return c.json({ ok: true, id, po_number: poNumber, revision, totals });
  });

  // POST /api/po/:id/supersede — clone a SENT PO into a new draft: supersede_seq+1,
  // revision/po_number reset (re-allocated at the clone's own generate), status 'draft',
  // supersedes_po_id → the source. The OLD PO is untouched here — it flips to 'superseded'
  // only when the successor reaches 'sent' (status-sync below), so the in-force PO stays
  // in force until its replacement actually ships (D7).
  app.post("/api/po/:id/supersede", gates.requireSession, gates.requireCapability(CAP_PO), async (c) => {
    const id = parseIdParam(c.req.param("id"));
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    const src = await c.env.DB.prepare("SELECT * FROM purchase_orders WHERE id = ?1").bind(id).first<Record<string, unknown>>();
    if (!src) return c.json({ error: "not_found" }, 404);
    // A change-order document can't be superseded (its clone would collide on
    // (change_order_of, co_seq), and re-issuing a CO is done as the NEXT CO against
    // the base — Track D2). Base documents only.
    if (src.status !== "sent" || src.change_order_of !== null) return c.json({ error: "not_supersedable" }, 409);
    // Operator-ratified 2026-08-15 (design-completion Q1, option A): a parent with ANY
    // non-canceled change order cannot be superseded — replacing it would strand paper that
    // amends it (drafts could never generate; sent COs would modify a void contract).
    // Further changes go out as the NEXT change order; the guard repeats atomically in the
    // clone INSERT's WHERE below.
    const liveCo = await c.env.DB
      .prepare("SELECT id FROM purchase_orders WHERE change_order_of = ?1 AND status != 'canceled' LIMIT 1")
      .bind(id)
      .first();
    if (liveCo) return c.json({ error: "has_change_orders" }, 409);
    // Double-submit guard (review finding, idempotency): if a live successor draft already
    // exists for this source, don't mint a sibling at the same supersede_seq — surface the
    // existing one. Canceled successors don't block a fresh supersede.
    const dup = await c.env.DB
      .prepare("SELECT id FROM purchase_orders WHERE supersedes_po_id = ?1 AND status != 'canceled'")
      .bind(id)
      .first<{ id: number }>();
    if (dup) return c.json({ error: "supersede_in_progress", existing_id: dup.id }, 409);

    const actor = c.get("session").username;
    const poUuid = crypto.randomUUID();
    // Clone parent + lines + audit in ONE batch (W4); the line clone is a single
    // INSERT..SELECT from the source's lines, po_id resolved via the po_uuid subquery
    // (constant per statement — see the create route's rationale).
    //
    // AUDIT ORDERING (review BLOCKER fix): auditStmtIfChanged's changes() guard reads the
    // IMMEDIATELY PRECEDING statement, so the audit stmt sits directly after the PARENT
    // clone INSERT (1 row iff the clone happened). Placed after the line-items INSERT it
    // read the line COUNT and silently skipped the audit row for every multi-line PO.
    const res = await c.env.DB.batch([
      c.env.DB
        .prepare(
          "INSERT INTO purchase_orders (po_uuid, job_no, site_phase, supersede_seq, job_id, job_name, " +
            "ship_to_name, ship_to_address, ship_to_city, ship_to_state, ship_to_zip, " +
            "delivery_contact_name, delivery_contact_phone, delivery_contact_email, " +
            "sow_text, delivery_instructions, payment_terms_text, terms_profile_id, terms_version, " +
            "subtotal_cents, tax_mode, tax_rate_bp, tax_cents, shipping_cents, total_cents, " +
            "line_column_variant, supersedes_po_id, status, approver_name, approver_title, vendor_key, created_by) " +
            "SELECT ?2, job_no, site_phase, supersede_seq + 1, job_id, job_name, " +
            "ship_to_name, ship_to_address, ship_to_city, ship_to_state, ship_to_zip, " +
            "delivery_contact_name, delivery_contact_phone, delivery_contact_email, " +
            "sow_text, delivery_instructions, payment_terms_text, terms_profile_id, terms_version, " +
            "subtotal_cents, tax_mode, tax_rate_bp, tax_cents, shipping_cents, total_cents, " +
            "line_column_variant, ?1, 'draft', approver_name, approver_title, vendor_key, ?3 " +
            "FROM purchase_orders WHERE id = ?1 AND status = 'sent' " +
            // Atomic twin of the has_change_orders pre-check above (operator-ratified Q1/A).
            "AND NOT EXISTS (SELECT 1 FROM purchase_orders co WHERE co.change_order_of = ?1 AND co.status != 'canceled')",
        )
        .bind(id, poUuid, actor),
      auditStmtIfChanged(c, actor, "po_supersede_clone", String(id), { source_po_id: id, po_uuid: poUuid }),
      c.env.DB
        .prepare(
          `INSERT INTO po_line_items (po_id, ${LINE_COLS}) ` +
            `SELECT (SELECT id FROM purchase_orders WHERE po_uuid = ?2), ${LINE_COLS} ` +
            "FROM po_line_items WHERE po_id = ?1 " +
            "AND EXISTS (SELECT 1 FROM purchase_orders WHERE po_uuid = ?2)",
        )
        .bind(id, poUuid),
    ]);
    if ((res[0].meta.changes ?? 0) === 0) return c.json({ error: "not_supersedable" }, 409); // lost race
    const clone = await c.env.DB.prepare("SELECT id FROM purchase_orders WHERE po_uuid = ?1").bind(poUuid).first<{ id: number }>();
    return c.json({ ok: true, id: clone?.id ?? null, supersedes_po_id: id }, 201);
  });

  // POST /api/po/:id/change-order — clone a SENT PO into a CHANGE-ORDER draft (Track D2).
  // The supersede clone's sibling with inverted semantics: the parent stays in force.
  // change_order_of → the source, supersedes_po_id stays NULL (the supersession flip
  // sites key on it, so a CO can never retire its parent), supersede_seq copied VERBATIM
  // (the CO lives beside the parent, not in a successor family), co_seq = MAX+1 over ALL
  // of the parent's COs (any status — a canceled generated CO's seq is never reused).
  // At generate the CO mints `{parent po_number}-CO{co_seq}` instead of allocating a
  // family revision. Gates: parent must be 'sent' AND itself a base document — a change
  // order to a change order is refused; corrections are the NEXT CO against the base.
  // Multiple concurrent CO drafts on one parent are allowed (unlike supersede's
  // one-in-progress guard): two independent changes are legitimate, and the partial
  // unique index (change_order_of, co_seq) is the allocation-race backstop.
  app.post("/api/po/:id/change-order", gates.requireSession, gates.requireCapability(CAP_PO), async (c) => {
    const id = parseIdParam(c.req.param("id"));
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    const src = await c.env.DB.prepare("SELECT * FROM purchase_orders WHERE id = ?1").bind(id).first<Record<string, unknown>>();
    if (!src) return c.json({ error: "not_found" }, 404);
    if (src.status !== "sent" || src.change_order_of !== null) {
      return c.json({ error: "not_change_orderable" }, 409);
    }

    const actor = c.get("session").username;
    const poUuid = crypto.randomUUID();
    // Same batch shape + audit ordering as the supersede clone above (audit stmt reads
    // changes() of the IMMEDIATELY PRECEDING statement — keep it directly after the
    // parent INSERT). estimate_id is deliberately not copied, matching supersede: the
    // import provenance belongs to the original document only.
    let res;
    try {
      res = await c.env.DB.batch([
        c.env.DB
          .prepare(
            "INSERT INTO purchase_orders (po_uuid, job_no, site_phase, supersede_seq, job_id, job_name, " +
              "ship_to_name, ship_to_address, ship_to_city, ship_to_state, ship_to_zip, " +
              "delivery_contact_name, delivery_contact_phone, delivery_contact_email, " +
              "sow_text, delivery_instructions, payment_terms_text, terms_profile_id, terms_version, " +
              "subtotal_cents, tax_mode, tax_rate_bp, tax_cents, shipping_cents, total_cents, " +
              "line_column_variant, change_order_of, co_seq, status, approver_name, approver_title, vendor_key, created_by) " +
              "SELECT ?2, job_no, site_phase, supersede_seq, job_id, job_name, " +
              "ship_to_name, ship_to_address, ship_to_city, ship_to_state, ship_to_zip, " +
              "delivery_contact_name, delivery_contact_phone, delivery_contact_email, " +
              // The scope declaration seeds as the FIRST LINE of the cloned sow_text
              // (operator-ratified Q3): delta semantics by default — the builder's radio
              // swaps it. sow_text is in the signed canonical, so the declaration the
              // vendor reads is signature-covered.
              "?4 || sow_text, delivery_instructions, payment_terms_text, terms_profile_id, terms_version, " +
              "subtotal_cents, tax_mode, tax_rate_bp, tax_cents, shipping_cents, total_cents, " +
              "line_column_variant, ?1, " +
              "(SELECT COALESCE(MAX(co_seq), 0) + 1 FROM purchase_orders WHERE change_order_of = ?1), " +
              "'draft', approver_name, approver_title, vendor_key, ?3 " +
              "FROM purchase_orders WHERE id = ?1 AND status = 'sent' AND change_order_of IS NULL",
          )
          .bind(id, poUuid, actor, `${CO_SCOPE_PO_DELTA}\n\n`),
        auditStmtIfChanged(c, actor, "po_change_order_clone", String(id), { source_po_id: id, po_uuid: poUuid }),
        c.env.DB
          .prepare(
            `INSERT INTO po_line_items (po_id, ${LINE_COLS}) ` +
              `SELECT (SELECT id FROM purchase_orders WHERE po_uuid = ?2), ${LINE_COLS} ` +
              "FROM po_line_items WHERE po_id = ?1 " +
              "AND EXISTS (SELECT 1 FROM purchase_orders WHERE po_uuid = ?2)",
          )
          .bind(id, poUuid),
      ]);
    } catch (e) {
      // Lost the co_seq allocation race (partial unique index) — nothing was written;
      // the client simply retries and reads a fresh MAX.
      if (isUniqueViolation(e)) return c.json({ error: "change_order_conflict" }, 409);
      throw e;
    }
    if ((res[0].meta.changes ?? 0) === 0) return c.json({ error: "not_change_orderable" }, 409); // lost race
    const clone = await c.env.DB
      .prepare("SELECT id, co_seq FROM purchase_orders WHERE po_uuid = ?1")
      .bind(poUuid)
      .first<{ id: number; co_seq: number }>();
    return c.json({ ok: true, id: clone?.id ?? null, change_order_of: id, co_seq: clone?.co_seq ?? null }, 201);
  });

  // POST /api/po/:id/cancel — off-path terminal, ONLY from draft/queued/pending_review
  // (an approved/sent PO is a live commercial document — superseding, not cancelling, is
  // its exit; a queued/pending_review cancel is honored Mac-side by the daemon's status
  // read before render/dispatch).
  app.post("/api/po/:id/cancel", gates.requireSession, gates.requireCapability(CAP_PO), async (c) => {
    const id = parseIdParam(c.req.param("id"));
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    const actor = c.get("session").username;
    const res = await c.env.DB.batch([
      c.env.DB
        .prepare(
          "UPDATE purchase_orders SET status='canceled', updated_at=unixepoch() " +
            "WHERE id=?1 AND status IN ('draft','queued','pending_review')",
        )
        .bind(id),
      auditStmtIfChanged(c, actor, "po_cancel", String(id), { po_id: id }),
    ]);
    if ((res[0].meta.changes ?? 0) === 0) {
      const row = await c.env.DB.prepare("SELECT status FROM purchase_orders WHERE id = ?1").bind(id).first();
      return row ? c.json({ error: "not_cancelable" }, 409) : c.json({ error: "not_found" }, 404);
    }
    return c.json({ ok: true, id });
  });

  // POST /api/po/:id/delete — HARD delete of an un-generated DRAFT (the row + its line items + its
  // attachment pool rows/chunks, Feature B). DRAFT-ONLY:
  // a generated/queued/pending_review/approved/sent/superseded/canceled row is a real record (PO number,
  // audit, possibly Box/Smartsheet artifacts) — those exit via cancel/supersede, NEVER a hard delete. Atomic:
  // every child delete is subquery-scoped to the parent STILL being a draft (no ON DELETE CASCADE), so
  // a non-draft leaves ALL tables untouched (no orphaned lines/attachments/chunks); the audit lands only if
  // the draft row was actually removed (changes()=1 on the parent delete, W4). CHILD ORDER is load-bearing:
  // chunks resolve through po_attachments, so they go BEFORE the attachment rows; the parent goes last.
  app.post("/api/po/:id/delete", gates.requireSession, gates.requireCapability(CAP_PO), async (c) => {
    const id = parseIdParam(c.req.param("id"));
    if (id === null) return c.json({ error: "invalid_id" }, 400);
    const actor = c.get("session").username;
    const res = await c.env.DB.batch([
      c.env.DB
        .prepare(
          "DELETE FROM po_attachment_chunks WHERE attachment_id IN " +
            "(SELECT id FROM po_attachments WHERE po_id IN (SELECT id FROM purchase_orders WHERE id=?1 AND status='draft'))",
        )
        .bind(id),
      c.env.DB
        .prepare(
          "DELETE FROM po_attachments WHERE po_id IN (SELECT id FROM purchase_orders WHERE id=?1 AND status='draft')",
        )
        .bind(id),
      c.env.DB
        .prepare(
          "DELETE FROM po_line_items WHERE po_id IN (SELECT id FROM purchase_orders WHERE id=?1 AND status='draft')",
        )
        .bind(id),
      c.env.DB.prepare("DELETE FROM purchase_orders WHERE id=?1 AND status='draft'").bind(id),
      auditStmtIfChanged(c, actor, "po_delete", String(id), { po_id: id }),
    ]);
    if ((res[3].meta.changes ?? 0) === 0) {
      const row = await c.env.DB.prepare("SELECT status FROM purchase_orders WHERE id = ?1").bind(id).first();
      // record_not_deletable, not not_deletable: the photo pool owns that code with
      // screening-specific copy — one errorCopy key was serving two meanings.
      return row ? c.json({ error: "record_not_deletable" }, 409) : c.json({ error: "not_found" }, 404);
    }
    return c.json({ ok: true, id });
  });

  // ══ Internal surface (requirePoToken — the Mac-side po_poll daemon) ═════════════

  // GET /api/po/internal/pending — the queue drain: queued POs + line items + hmac,
  // oldest-first. The daemon recomputes the po:v1 canonical HMAC before trusting a row
  // (shared/portal_hmac.py PO_DOMAIN, S4) — same pull-model trust chain as submissions.
  app.get("/api/po/internal/pending", gates.requirePoToken, async (c) => {
    const limit = Math.min(Math.max(parseInt(c.req.query("limit") || "50", 10) || 50, 1), PO_PENDING_CAP);
    const { results } = await c.env.DB
      .prepare("SELECT * FROM purchase_orders WHERE status = 'queued' ORDER BY updated_at ASC, id ASC LIMIT ?1")
      .bind(limit)
      .all<Record<string, unknown>>();
    const rows = (results ?? []) as Record<string, unknown>[];
    for (const r of rows) r.line_items = await loadLines(c.env.DB, r.id as number);
    return c.json({ pending: rows });
  });

  // POST /api/po/internal/mark-filed — the receipt: queued→pending_review + box_file_id,
  // after the daemon has verified/rendered/filed (Box + PO_Log + PO_Pending_Review).
  // Idempotent: a replay (already pending_review) is a no-op — ok:true, found:false —
  // and the guarded audit writes nothing on the no-op.
  app.post("/api/po/internal/mark-filed", gates.requirePoToken, async (c) => {
    let body: Record<string, unknown>;
    try {
      body = (await c.req.json()) as Record<string, unknown>;
    } catch {
      return c.json({ error: "bad_request" }, 400);
    }
    if (!isPlainObject(body)) return c.json({ error: "bad_request" }, 400);
    const poId = typeof body.po_id === "number" && Number.isSafeInteger(body.po_id) && body.po_id > 0 ? body.po_id : null;
    const boxFileId = typeof body.box_file_id === "string" ? body.box_file_id.slice(0, 200) : null;
    if (poId === null) return c.json({ error: "invalid_po_id" }, 400);
    const res = await c.env.DB.batch([
      c.env.DB
        .prepare(
          "UPDATE purchase_orders SET status='pending_review', box_file_id=?2, updated_at=unixepoch() " +
            "WHERE id=?1 AND status='queued'",
        )
        .bind(poId, boxFileId),
      auditStmtIfChanged(c, SYSTEM_ACTOR, "po_mark_filed", String(poId), { po_id: poId, box_file_id: boxFileId }),
    ]);
    return c.json({ ok: true, found: (res[0].meta.changes ?? 0) > 0 });
  });

  // POST /api/po/internal/status-sync — Mac-side machine outcomes (approved/sent/superseded
  // stamps from F22 approval + the send poller). D1 status here is a CACHE of the
  // Mac/Smartsheet-side authoritative state; the in-WHERE guards exist to prevent REGRESSION
  // (a stale/replayed sync can never move a status backwards), not to re-enforce F22 — the
  // real approval gate is Mac-side. SUPERSESSION FLIP: when a PO reaches 'sent' and carries
  // supersedes_po_id, the superseded predecessor flips in the SAME batch, guarded on the
  // successor actually being 'sent' at execution time (the scalar subquery re-checks D1
  // state after the sent-UPDATE in this batch — atomic, race-proof).
  app.post("/api/po/internal/status-sync", gates.requirePoToken, async (c) => {
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
    if (raw.length > PO_STATUS_SYNC_CAP) return c.json({ error: "too_many_updates" }, 413);

    const statements = [];
    const touched: string[] = [];
    for (const u of raw) {
      if (!isPlainObject(u)) return c.json({ error: "invalid_update" }, 400);
      const poId = typeof u.po_id === "number" && Number.isSafeInteger(u.po_id) && u.po_id > 0 ? u.po_id : null;
      const status = typeof u.status === "string" && SYNCABLE_STATUSES.has(u.status) ? u.status : null;
      if (poId === null || status === null) return c.json({ error: "invalid_update" }, 400);
      if (status === "approved") {
        statements.push(
          c.env.DB
            .prepare("UPDATE purchase_orders SET status='approved', updated_at=unixepoch() WHERE id=?1 AND status='pending_review'")
            .bind(poId),
          auditStmtIfChanged(c, SYSTEM_ACTOR, "po_status_approved", String(poId), { po_id: poId }),
        );
      } else if (status === "sent") {
        statements.push(
          c.env.DB
            .prepare("UPDATE purchase_orders SET status='sent', updated_at=unixepoch() WHERE id=?1 AND status='approved'")
            .bind(poId),
          auditStmtIfChanged(c, SYSTEM_ACTOR, "po_status_sent", String(poId), { po_id: poId }),
          // The supersession flip — same batch (W4): the predecessor goes 'superseded' IFF
          // this PO is NOW 'sent' in D1 (re-checked at execution time, after the UPDATE
          // above) and the predecessor is still the in-force 'sent' document.
          c.env.DB
            .prepare(
              "UPDATE purchase_orders SET status='superseded', updated_at=unixepoch() " +
                "WHERE id = (SELECT supersedes_po_id FROM purchase_orders WHERE id = ?1) " +
                "AND status = 'sent' " +
                "AND (SELECT status FROM purchase_orders WHERE id = ?1) = 'sent'",
            )
            .bind(poId),
          auditStmtIfChanged(c, SYSTEM_ACTOR, "po_superseded_flip", String(poId), { successor_po_id: poId }),
        );
      } else {
        // Manual 'superseded' stamp (an operator-driven Mac-side supersession record).
        statements.push(
          c.env.DB
            .prepare("UPDATE purchase_orders SET status='superseded', updated_at=unixepoch() WHERE id=?1 AND status='sent'")
            .bind(poId),
          auditStmtIfChanged(c, SYSTEM_ACTOR, "po_status_superseded", String(poId), { po_id: poId }),
        );
      }
      touched.push(`${poId}:${status}`);
    }
    await c.env.DB.batch(statements);
    return c.json({ ok: true, updated: touched.length });
  });

  // POST /api/po/internal/vendors/sync — the Smartsheet→D1 full-replace down-sync (D4/§51).
  // UPSERT every supplied row EXCEPT dirty ones: the conflict-UPDATE carries
  // `WHERE po_vendors.sync_state != 'pending'` — THE dirty-row fence — so an un-mirrored
  // portal edit is never clobbered by the sheet state (it up-syncs first, then the next
  // down-sync converges). Refuses an empty payload (a Smartsheet read-miss must never wipe
  // the cache); NEVER deletes — a sheet-retired vendor arrives with active=0. Watermarks
  // (mirror_version/mirrored_version) are deliberately untouched: they are the UP-sync's
  // bookkeeping.
  app.post("/api/po/internal/vendors/sync", gates.requirePoToken, async (c) => {
    let body: { vendors?: unknown };
    try {
      body = await c.req.json();
    } catch {
      return c.json({ error: "bad_request" }, 400);
    }
    if (!isPlainObject(body)) return c.json({ error: "bad_request" }, 400);
    const raw = body.vendors;
    if (!Array.isArray(raw)) return c.json({ error: "invalid_vendors" }, 400);
    if (raw.length === 0) return c.json({ error: "empty_vendors" }, 400);
    if (raw.length > MAX_SYNC_ROWS) return c.json({ error: "too_many_vendors" }, 413);

    // Validate + normalize every row up front; reject the WHOLE batch on any bad row
    // (a partial sync would silently desync the cache — the /api/internal/sync posture).
    const rows: (VendorFields & { vendor_key: string })[] = [];
    const seen = new Set<string>();
    for (const r of raw) {
      if (!isPlainObject(r)) return c.json({ error: "invalid_row" }, 400);
      const vendor_key = str(r.vendor_key);
      if (!VENDOR_KEY_RE.test(vendor_key)) return c.json({ error: "invalid_row" }, 400);
      if (seen.has(vendor_key)) return c.json({ error: "duplicate_vendor_key" }, 400);
      seen.add(vendor_key);
      const f = parseVendorFields(r);
      if (typeof f === "string") return c.json({ error: "invalid_row", field: f }, 400);
      rows.push({ vendor_key, ...f });
    }

    const statements = rows.map((v) =>
      c.env.DB
        .prepare(
          "INSERT INTO po_vendors (vendor_key, vendor_name, address, contact_name, contact_email, " +
            "contact_phone, region, supply_categories, default_terms_profile, gtc_reference, active, notes, " +
            "origin, sync_state, mirror_version, mirrored_version) " +
            "VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,'smartsheet','synced',0,0) " +
            "ON CONFLICT(vendor_key) DO UPDATE SET " +
            "vendor_name=excluded.vendor_name, address=excluded.address, contact_name=excluded.contact_name, " +
            "contact_email=excluded.contact_email, contact_phone=excluded.contact_phone, region=excluded.region, " +
            "supply_categories=excluded.supply_categories, default_terms_profile=excluded.default_terms_profile, " +
            "gtc_reference=excluded.gtc_reference, active=excluded.active, notes=excluded.notes, " +
            "origin='smartsheet', updated_at=unixepoch() " +
            "WHERE po_vendors.sync_state != 'pending'", // ← THE dirty-row fence
        )
        .bind(
          v.vendor_key, v.vendor_name, v.address, v.contact_name, v.contact_email, v.contact_phone,
          v.region, v.supply_categories, v.default_terms_profile, v.gtc_reference, v.active, v.notes,
        ),
    );
    statements.push(
      c.env.DB
        .prepare("INSERT INTO audit_log (actor_username, action, target_username, detail) VALUES (?1,?2,?3,?4)")
        .bind(SYSTEM_ACTOR, "po_vendors_sync", "", JSON.stringify({ supplied: rows.length })),
    );
    const results = await c.env.DB.batch(statements);
    // Per-row meta.changes: 1 = inserted/updated, 0 = fenced (dirty row skipped).
    let upserted = 0;
    for (let i = 0; i < rows.length; i++) if ((results[i]?.meta?.changes ?? 0) > 0) upserted++;
    return c.json({ ok: true, upserted, skipped_dirty: rows.length - upserted });
  });

  // GET /api/po/internal/vendors/pending — the up-sync read: portal-edited (dirty) rows +
  // the version vector. The daemon bridge-key find-or-creates the ITS_Vendors row by
  // vendor_key, then commits via mark-mirrored below.
  app.get("/api/po/internal/vendors/pending", gates.requirePoToken, async (c) => {
    const { results } = await c.env.DB
      .prepare(
        "SELECT vendor_key, vendor_name, address, contact_name, contact_email, contact_phone, " +
          "region, supply_categories, default_terms_profile, gtc_reference, active, notes, " +
          "origin, mirror_version, mirrored_version " +
          "FROM po_vendors WHERE sync_state = 'pending' ORDER BY mirror_version ASC, vendor_key ASC LIMIT ?1",
      )
      .bind(VENDOR_PENDING_CAP)
      .all<Record<string, unknown>>();
    const vendors = (results ?? []).map((v) => ({ ...v, supply_categories: parseJsonArray(v.supply_categories) }));
    return c.json({ vendors });
  });

  // POST /api/po/internal/vendors/mark-mirrored — the up-sync commit point:
  // pending→synced + mirrored_version=mirror_version, ONLY IF mirror_version is UNCHANGED
  // since the daemon's pending read (the watermark guard, bound in-WHERE) — a portal edit
  // racing the mirror bumps mirror_version, the guard fails, the row STAYS pending and
  // re-up-syncs next cycle. Idempotent: a replay of a satisfied update is a no-op.
  app.post("/api/po/internal/vendors/mark-mirrored", gates.requirePoToken, async (c) => {
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
    if (raw.length > VENDOR_PENDING_CAP) return c.json({ error: "too_many_updates" }, 413);

    const updates: { vendor_key: string; mirrored_version: number }[] = [];
    for (const u of raw) {
      if (!isPlainObject(u)) return c.json({ error: "invalid_update" }, 400);
      const vendor_key = str(u.vendor_key);
      const version = typeof u.mirrored_version === "number" && Number.isSafeInteger(u.mirrored_version) && u.mirrored_version >= 1
        ? u.mirrored_version
        : null;
      if (!VENDOR_KEY_RE.test(vendor_key) || version === null) return c.json({ error: "invalid_update" }, 400);
      updates.push({ vendor_key, mirrored_version: version });
    }
    const statements = updates.map((u) =>
      c.env.DB
        .prepare(
          "UPDATE po_vendors SET mirrored_version=?2, sync_state='synced', updated_at=unixepoch() " +
            "WHERE vendor_key=?1 AND sync_state='pending' AND mirror_version=?2",
        )
        .bind(u.vendor_key, u.mirrored_version),
    );
    statements.push(
      c.env.DB
        .prepare("INSERT INTO audit_log (actor_username, action, target_username, detail) VALUES (?1,?2,?3,?4)")
        .bind(SYSTEM_ACTOR, "po_vendors_mark_mirrored", "", JSON.stringify({
          count: updates.length,
          keys: updates.slice(0, 50).map((u) => u.vendor_key),
        })),
    );
    const results = await c.env.DB.batch(statements);
    let flipped = 0;
    for (let i = 0; i < updates.length; i++) if ((results[i]?.meta?.changes ?? 0) > 0) flipped++;
    return c.json({ ok: true, flipped, stale: updates.length - flipped });
  });
}
