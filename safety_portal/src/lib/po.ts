// Purchase-Order API client (S6, Aug-7 delivery program WS1). Same-origin cookie fetch;
// every route is session + cap.po.manage gated server-side (worker/po.ts) — the SPA gating
// is convenience, never the boundary (Invariant 2).
//
// MONEY IS INTEGER CENTS EVERYWHERE (D8). The helpers below mirror the Worker's integer
// math (worker/po.ts lineExtendedCents / computeTotals) for DISPLAY ONLY — the Worker
// recomputes all money server-side and the generate route hard-409s (`totals_mismatch`,
// carrying `recomputed`) when the client's displayed totals disagree. The Worker's numbers
// are always authoritative; the mirror exists so the office admin watches live totals while
// typing, not so the client is ever trusted.
//
// GET /api/po/terms and GET /api/po/config land in PR #495 (the S3 Worker read surface);
// the shapes here are built against that PR's contract.

import { ApiError, raiseApiError } from "./errorCopy";

// ── Wire shapes (mirror worker/po.ts) ────────────────────────────────────────────────────────────

export interface Vendor {
  vendor_key: string;
  vendor_name: string;
  address: string;
  contact_name: string;
  contact_email: string;
  contact_phone: string;
  region: string;
  supply_categories: string[];
  default_terms_profile: string;
  gtc_reference: string;
  active: number;
  notes: string;
  origin: string;
  /** §51 up-sync state: 'pending' = a portal edit not yet mirrored to ITS_Vendors. */
  sync_state: string;
  mirror_version: number;
}

/** The writable vendor fields (create + update share the shape; worker parseVendorFields). */
export interface VendorFields {
  vendor_name: string;
  address?: string;
  contact_name?: string;
  contact_email?: string;
  contact_phone?: string;
  region?: string;
  supply_categories?: string[];
  default_terms_profile?: string;
  gtc_reference?: string;
  notes?: string;
  /** 1 = live, 0 = deactivated (never a delete — D4). Omitted = keep-live. */
  active?: number;
}

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

export interface PoTotals {
  subtotal_cents: number;
  tax_rate_bp: number;
  tax_cents: number;
  total_cents: number;
}

export type PoStatus =
  | "draft"
  | "queued"
  | "pending_review"
  | "approved"
  | "sent"
  | "superseded"
  | "canceled";

/** One row of GET /api/po/pos — the tracker list projection. */
export interface PoListRow {
  id: number;
  po_number: string | null;
  job_no: string;
  site_phase: number;
  supersede_seq: number;
  revision: number | null;
  vendor_key: string;
  job_id: string;
  job_name: string;
  status: PoStatus;
  total_cents: number;
  supersedes_po_id: number | null;
  /** Track D2: set when this PO IS a change order against `change_order_of`; its number is
   *  `{parent number}-CO{co_seq}`, minted at generate. The parent stays in force. */
  change_order_of: number | null;
  co_seq: number | null;
  box_file_id: string | null;
  created_by: string;
  created_at: number;
  updated_at: number;
}

/** GET /api/po/pos/:id serves the FULL row (SELECT *) — the draft-edit / preview read. */
export interface PoDetail extends PoListRow {
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
  line_column_variant: string;
  approver_name: string;
  approver_title: string;
}

/** One client line in a draft body (positions + extended are SERVER-assigned/ignored). */
export interface DraftLine {
  part_number?: string;
  description: string;
  qty: number;
  unit?: string;
  unit_cost_cents?: number | null;
  watts?: number | null;
  panels?: number | null;
  pallets?: number | null;
  price_per_watt_microcents?: number | null;
}

export type LineColumnVariant = "default" | "lump_sum" | "per_watt";
export type TaxMode = "auto" | "exempt" | "included" | "override";

/** POST /api/po/drafts (+ /:id/update) body — mirrors worker parseDraftBody. */
export interface DraftBody {
  vendor_key: string;
  job_no: string;
  site_phase: number;
  job_id?: string;
  job_name?: string;
  ship_to_name?: string;
  ship_to_address?: string;
  ship_to_city?: string;
  ship_to_state?: string;
  ship_to_zip?: string;
  delivery_contact_name?: string;
  delivery_contact_phone?: string;
  delivery_contact_email?: string;
  sow_text?: string;
  delivery_instructions?: string;
  payment_terms_text?: string;
  terms_profile_id?: string;
  terms_version?: string;
  tax_mode?: TaxMode;
  tax_rate_bp?: number;
  shipping_cents?: number;
  line_column_variant?: LineColumnVariant;
  approver_name?: string;
  approver_title?: string;
  /** ADR-0004 estimate-import provenance: the po_estimates row this draft was imported
   *  from. STORE-ONLY server-side (never enters the po:v1 HMAC); the draft route refuses
   *  409 `estimate_already_imported` when a non-canceled PO already carries it. */
  estimate_id?: number;
  line_items: DraftLine[];
}

/** GET /api/po/terms (PR #495). `render_line` only on attach-kind profiles. */
export interface TermsProfile {
  id: string;
  kind: "library" | "attach";
  label: string;
  description: string;
  current_version: string | null;
  tokens: string[];
  render_line: string | null;
}

/** One configured delivery contact (Feature C) — a suggestion for the builder's
 *  delivery-contact name <datalist>; an EXACT name match auto-fills phone + email.
 *  phone/email ride optional on the wire (the config validator normalizes them to ""). */
export interface DeliveryContact {
  name: string;
  phone?: string;
  email?: string;
}

/** GET /api/po/config (PR #495) — versioned purchaser identity + the D8 tax table +
 *  the configured delivery-contact suggestion list (Feature C). */
export interface PoConfig {
  purchaser: {
    entity: string;
    address_lines: string[];
    phone: string;
    invoice_routing: { to: string; cc: string[] };
  };
  tax: {
    /** Basis points by 2-letter ship-to state (900 = 9%). */
    rates_bp: Record<string, number>;
    state_names: Record<string, string>;
  };
  /** Suggestions only — free-text delivery contacts stay accepted on every draft. */
  delivery_contacts: DeliveryContact[];
}

// ── Fetch helpers ────────────────────────────────────────────────────────────────────────────────

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { credentials: "same-origin" });
  if (!res.ok) return raiseApiError(res);
  return (await res.json()) as T;
}

async function postJson<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) return raiseApiError(res);
  return (await res.json()) as T;
}

// ── Vendors ──────────────────────────────────────────────────────────────────────────────────────

export async function fetchVendors(includeInactive = false): Promise<Vendor[]> {
  const q = includeInactive ? "?include_inactive=1" : "";
  const data = await getJson<{ vendors: Vendor[] }>(`/api/po/vendors${q}`);
  return data.vendors ?? [];
}

export async function createVendor(fields: VendorFields): Promise<{ vendor_key: string }> {
  return postJson<{ ok: boolean; vendor_key: string }>("/api/po/vendors", fields);
}

export async function updateVendor(vendorKey: string, fields: VendorFields): Promise<void> {
  await postJson(`/api/po/vendors/${encodeURIComponent(vendorKey)}/update`, fields);
}

// ── Job ship-to auto-fill (S6 follow-up) ───────────────────────────────────────────────────────────

/** The builder's ship-to + delivery auto-fill block for a job, from the routing SoR under
 *  session + cap.po.manage (GET /api/po/jobs/:job_id/ship-to). city/state/zip come back empty
 *  today (the SoR carries a single `address` line — see the Worker route). */
export interface JobShipTo {
  job_id: string;
  job_no: string;
  /** 0064 — the Evergreen identifier's third segment, so the builder's Site/phase input
   *  auto-fills from the job instead of the operator re-deriving it. 0 = no site. */
  site_phase: number;
  ship_to_name: string;
  ship_to_address: string;
  ship_to_city: string;
  ship_to_state: string;
  ship_to_zip: string;
  delivery_contact_name: string;
  delivery_contact_phone: string;
  delivery_contact_email: string;
}

/** Fetch a job's ship-to auto-fill block. Throws ApiError on 404/non-2xx; the builder catches
 *  and silently degrades — auto-fill is a convenience, every field stays operator-editable. */
export async function fetchJobShipTo(jobId: string): Promise<JobShipTo> {
  return getJson<JobShipTo>(`/api/po/jobs/${encodeURIComponent(jobId)}/ship-to`);
}

// ── POs ──────────────────────────────────────────────────────────────────────────────────────────

export async function fetchPos(status?: PoStatus): Promise<PoListRow[]> {
  const q = status ? `?status=${encodeURIComponent(status)}` : "";
  const data = await getJson<{ pos: PoListRow[] }>(`/api/po/pos${q}`);
  return data.pos ?? [];
}

export async function fetchPo(id: number): Promise<{ po: PoDetail; line_items: PoLine[] }> {
  return getJson(`/api/po/pos/${id}`);
}

export async function createDraft(body: DraftBody): Promise<{ id: number; totals: PoTotals }> {
  return postJson<{ ok: boolean; id: number; totals: PoTotals }>("/api/po/drafts", body);
}

export async function updateDraft(id: number, body: DraftBody): Promise<{ id: number; totals: PoTotals }> {
  return postJson<{ ok: boolean; id: number; totals: PoTotals }>(`/api/po/drafts/${id}/update`, body);
}

/** The generate outcomes the wizard branches on. `totals_mismatch` carries the server's
 *  `recomputed` totals — the SPA re-renders FROM those (the Worker is authoritative) and the
 *  admin generates again. `draft_changed` / `po_number_conflict` / `not_draft` are the other
 *  contractual 409s (worker/po.ts generate); anything else throws ApiError. */
export type GenerateResult =
  | { ok: true; id: number; po_number: string; revision: number; totals: PoTotals }
  | { ok: false; error: "totals_mismatch"; recomputed: PoTotals }
  | { ok: false; error: "draft_changed" | "po_number_conflict" | "not_draft" };

export async function generateDraft(
  id: number,
  displayed: { subtotal_cents: number; tax_cents: number; total_cents: number },
): Promise<GenerateResult> {
  const res = await fetch(`/api/po/drafts/${id}/generate`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      subtotal_cents: displayed.subtotal_cents,
      tax_cents: displayed.tax_cents,
      total_cents: displayed.total_cents,
    }),
  });
  if (res.ok) {
    const data = (await res.json()) as { id: number; po_number: string; revision: number; totals: PoTotals };
    return { ok: true, ...data };
  }
  const body = (await res.json().catch(() => ({}))) as { error?: string; recomputed?: PoTotals };
  if (body.error === "totals_mismatch" && body.recomputed) {
    return { ok: false, error: "totals_mismatch", recomputed: body.recomputed };
  }
  if (body.error === "draft_changed" || body.error === "po_number_conflict" || body.error === "not_draft") {
    return { ok: false, error: body.error };
  }
  throw new ApiError(body.error ?? null, res.status);
}

/** Supersede a SENT PO: the Worker clones it into a new draft (supersede_seq+1). A live
 *  successor already in flight comes back as `supersede_in_progress` + its id — the SPA opens
 *  THAT draft instead of minting a sibling. */
export type SupersedeResult =
  | { ok: true; id: number }
  | { ok: false; error: "supersede_in_progress"; existing_id: number };

export async function supersedePo(id: number): Promise<SupersedeResult> {
  const res = await fetch(`/api/po/${id}/supersede`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({}),
  });
  if (res.ok) {
    const data = (await res.json()) as { id: number };
    return { ok: true, id: data.id };
  }
  const body = (await res.json().catch(() => ({}))) as { error?: string; existing_id?: number };
  if (body.error === "supersede_in_progress" && typeof body.existing_id === "number") {
    return { ok: false, error: "supersede_in_progress", existing_id: body.existing_id };
  }
  throw new ApiError(body.error ?? null, res.status);
}

/** Create a CHANGE-ORDER draft against a SENT PO (Track D2): the Worker clones the parent's
 *  full configuration + lines into a fresh draft linked change_order_of/co_seq. The parent
 *  stays in force; the CO's number (`{parent}-CO{n}`) is minted at its own generate. Failures
 *  (wrong state, CO-of-CO, a lost co_seq race) throw ApiError for errorCopy translation. */
export async function createPoChangeOrder(id: number): Promise<{ id: number; co_seq: number | null }> {
  const res = await fetch(`/api/po/${id}/change-order`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({}),
  });
  if (res.ok) {
    const data = (await res.json()) as { id: number; co_seq: number | null };
    return { id: data.id, co_seq: data.co_seq ?? null };
  }
  const body = (await res.json().catch(() => ({}))) as { error?: string };
  throw new ApiError(body.error ?? null, res.status);
}

export async function cancelPo(id: number): Promise<void> {
  await postJson(`/api/po/${id}/cancel`, {});
}

/** HARD-delete an un-generated DRAFT PO (row + line items + attachments). Draft-only; a generated record is 409 record_not_deletable. */
export async function deletePoDraft(id: number): Promise<void> {
  await postJson(`/api/po/${id}/delete`, {});
}

// ── Document attachments (Feature B) ─────────────────────────────────────────────────────────────

/** One attachment row as the LIST route projects it — metadata only, never bytes
 *  (§34 Option D: bytes flow Mac-ward over the internal bearer exclusively). */
export interface PoAttachment {
  id: number;
  filename: string;
  declared_mime: string;
  size_bytes: number;
  status: "pending" | "claimed" | "filed" | "refused";
  created_at: number;
}

/** The client-side mirror of the Worker's upload bounds (po_attachments.ts) — HINTS
 *  only; the Worker re-gates every upload (Invariant 2: SPA gating is convenience). */
export const ATTACHMENT_MAX_BYTES = 10_000_000;
export const MAX_ATTACHMENTS_PER_PO = 5;
export const ATTACHMENT_ACCEPT = ".pdf,.jpg,.jpeg,.png,.docx,.xlsx";
export const ATTACHMENT_MIME_BY_EXT: Record<string, string> = {
  ".pdf": "application/pdf",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".png": "image/png",
  ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
};

export async function fetchPoAttachments(poId: number): Promise<PoAttachment[]> {
  const data = await getJson<{ attachments: PoAttachment[] }>(`/api/po/pos/${poId}/attachments`);
  return data.attachments ?? [];
}

/** Upload one attachment onto a DRAFT (base64 in JSON — the photo wire). The Worker
 *  bounds-gates (size/count/filename/MIME allowlist + magic sniff) and pools the bytes
 *  in D1 for the Mac-side §34 screen; nothing reaches Box until the screen passes. */
export async function uploadPoAttachment(
  poId: number, filename: string, mime: string, dataB64: string,
): Promise<{ id: number }> {
  return postJson<{ ok: boolean; id: number }>(`/api/po/drafts/${poId}/attachments`, {
    filename, mime, data_b64: dataB64,
  });
}

/** Remove an attachment from a DRAFT (row + chunks). Draft-only — 409 once generated. */
export async function deletePoAttachment(poId: number, attachmentId: number): Promise<void> {
  await postJson(`/api/po/drafts/${poId}/attachments/${attachmentId}/delete`, {});
}

// ── Terms + config (S3 read surface, PR #495) ────────────────────────────────────────────────────

export async function fetchTerms(): Promise<TermsProfile[]> {
  const data = await getJson<{ profiles: TermsProfile[] }>("/api/po/terms");
  return data.profiles ?? [];
}

/** GET /api/po/terms/:id/text — a library profile's CURRENT version clause body (header-stripped),
 *  for the editor's edit-text pre-fill. Attach/unknown profiles 404. */
export interface TermsText {
  profile_id: string;
  version: string;
  text: string;
}
export async function fetchTermsText(profileId: string): Promise<TermsText> {
  return getJson<TermsText>(`/api/po/terms/${encodeURIComponent(profileId)}/text`);
}

/** GET /api/po/terms/:id/versions — the version list (id + legal_review) for the make-current picker;
 *  file names / sha256 stay off the wire. */
export interface TermsVersionRow {
  version: string;
  legal_review: string;
}
export interface TermsVersions {
  profile_id: string;
  current_version: string | null;
  versions: TermsVersionRow[];
}
export async function fetchTermsVersions(profileId: string): Promise<TermsVersions> {
  return getJson<TermsVersions>(`/api/po/terms/${encodeURIComponent(profileId)}/versions`);
}

export async function fetchPoConfig(): Promise<PoConfig> {
  return getJson<PoConfig>("/api/po/config");
}

// ── Config EDITOR (§50 send-free enqueue + status monitor, slice 1 routes) ─────────────────────────
//
// The browser front of the §50 privileged-code-actuation pipeline. `submitConfigEdit` POSTs a config
// change to the send-free cloud queue (POST /api/config/requests) — the Worker only VALIDATES +
// ENQUEUES it in D1; the Mac config daemon is the sole actuator that git-commits + deploys the value.
// The SPA can only queue, never commit (Invariant 1). `fetchConfigStatus` reads the queue back so the
// admin watches each edit advance queued→validated→tested→live (or fail, never silently). Both routes
// are re-gated per-workstream server-side against the resolved artifact's capability (Invariant 2).

/** The config ops: `edit` replaces a JSON artifact's value (purchaser / tax); `add_version` mints a
 *  new sha-pinned terms version (ships legal_review: pending — the deliberate legal gate);
 *  `set_current` makes an existing terms version live (clears its legal_review + repoints
 *  current_version) — the operator's confirmable legal-activation action. */
export type ConfigOp = "edit" | "add_version" | "set_current" | "create_profile";

/** POST /api/config/requests body (worker/config.ts). `payload` is the full artifact value;
 *  `target_version` rides ONLY an add_version (a lowercase [a-z0-9_] slug, e.g. standard_17_v2). */
export interface ConfigEditBody {
  workstream: string;
  artifact_key: string;
  op: ConfigOp;
  payload: unknown;
  target_version?: string;
}

/** 201 enqueue result — the row lands `queued`; the daemon advances it from there. */
export interface ConfigEnqueueResult {
  ok: boolean;
  id: number | null;
  status: "queued";
}

/** The config-request lifecycle (LOCKSTEP with worker CONFIG_STATUSES / migration 0045). `merged`
 *  is a transient stage between tested and live; `archived` is terminal success; `failed` is terminal
 *  failure and NEVER silent (carries failed_stage + failure_reason, surfaced verbatim). */
export type ConfigStatus = "queued" | "validated" | "tested" | "merged" | "live" | "archived" | "failed";

/** One row of GET /api/config/requests/status — scoped server-side to the caller's held workstreams. */
export interface ConfigRequest {
  id: number;
  workstream: string;
  artifact_key: string;
  op: ConfigOp;
  status: ConfigStatus;
  failed_stage: string | null;
  failure_reason: string | null;
  created_at: number;
  updated_at: number;
  /** Soft-dismiss timestamp (migration 0047): non-null once cleared. Absent from the DEFAULT monitor
   *  view (those rows are filtered server-side); present only under ?include_cleared=1. */
  cleared_at?: number | null;
}

/** Enqueue a config edit (send-free). Throws ApiError on a non-2xx — the page surfaces `.message`
 *  (human copy from errorCopy) so a rejected edit is never silent. */
export async function submitConfigEdit(body: ConfigEditBody): Promise<ConfigEnqueueResult> {
  return postJson<ConfigEnqueueResult>("/api/config/requests", body);
}

/** Read the config-request queue back (most-recent first, ≤50, scoped to held workstreams). */
export async function fetchConfigStatus(): Promise<ConfigRequest[]> {
  const data = await getJson<{ requests: ConfigRequest[] }>("/api/config/requests/status");
  return data.requests ?? [];
}

/** Soft-dismiss (clear) a TERMINAL config request (live/archived/failed) from the status monitor.
 *  Forensic-safe: the config_requests row persists in D1 — this only hides it from the default view.
 *  Throws ApiError on a non-2xx (e.g. 409 config_not_terminal for an in-flight row, 403 for a
 *  workstream the caller doesn't manage). Send-free; the SPA never advances the queue. */
export async function clearConfigRequest(id: number): Promise<{ ok: boolean; cleared: boolean }> {
  return postJson<{ ok: boolean; cleared: boolean }>(`/api/config/requests/${id}/clear`, {});
}

/** "9.25" (percent, ≤2 dp) → 925 integer basis points; null on anything unparseable or >100%.
 *  String math, NEVER parseFloat×100 (mirrors parseDollarsToCents / the bpToPct display in reverse):
 *  1 bp = 0.01%, so a percent with >2 decimals would be a non-integer bp and is rejected as a hint.
 *  The Worker + config actuator re-validate — this is a client-side convenience, never the boundary. */
export function pctToBp(input: string): number | null {
  const t = input.trim().replace(/%$/, "");
  if (!/^\d+(\.\d{1,2})?$/.test(t)) return null;
  const [d, f = ""] = t.split(".");
  const bp = parseInt(d, 10) * 100 + (f ? parseInt(f.padEnd(2, "0"), 10) : 0);
  if (!Number.isSafeInteger(bp) || bp > 10_000) return null; // cap at 100%
  return bp;
}

// ── Material catalog picker (GET /api/po/materials) ─────────────────────────────────────────────

/** One active row from GET /api/po/materials — a thin read of the SAME material_catalog TYPE
 *  table the field-ops Materials Catalog admin manages (migration 0019), gated cap.po.manage.
 *  It's a TYPE vocabulary (manufacturer / model / specs) with NO price, so a pick populates a
 *  line's IDENTITY only — qty/unit/unit_cost stay per-PO operator entry. */
export interface CatalogMaterial {
  id: number;
  model_id: string;
  manufacturer: string | null;
  category: string;
  key_specs: string | null;
}

export async function fetchPoMaterials(category?: string): Promise<CatalogMaterial[]> {
  const q = category ? `?category=${encodeURIComponent(category)}` : "";
  const data = await getJson<{ materials: CatalogMaterial[] }>(`/api/po/materials${q}`);
  return data.materials ?? [];
}

/** Project a catalog TYPE onto a PO line's identity fields. part_number ← model_id;
 *  description ← "manufacturer model_id — key_specs" (each part optional). Both are sliced to
 *  the Worker's line bounds (worker/po.ts MAX_SHORT=64 / MAX_LINE_TEXT=512) so a pick can never
 *  trip a 400 — real seed data is well under, this is a defensive cap. The catalog carries no
 *  price, so qty/unit/unit_cost are deliberately left for the operator to enter per-PO. */
export function catalogLineFields(m: CatalogMaterial): { part_number: string; description: string } {
  const head = [m.manufacturer?.trim(), m.model_id.trim()].filter(Boolean).join(" ");
  const specs = m.key_specs?.trim();
  const description = specs ? `${head} — ${specs}` : head;
  return {
    part_number: m.model_id.trim().slice(0, 64),
    description: description.slice(0, 512),
  };
}

// ── Shared vendor vocabulary (ITS_Vendors sheet schema — build_its_vendors_sheet.py) ─────────────

/** The 13 supply categories — the ITS_Vendors MULTI_PICKLIST vocabulary. Keys are the wire
 *  values; labels are display-only. Both PO pages (vendor management + the builder's filter
 *  chips) consume this one list. */
export const SUPPLY_CATEGORIES: [string, string][] = [
  ["modules", "Modules"],
  ["racking", "Racking"],
  ["inverters", "Inverters"],
  ["electrical_bos", "Electrical BOS"],
  ["wire", "Wire"],
  ["switchgear", "Switchgear"],
  ["combiners", "Combiners"],
  ["transformers", "Transformers"],
  ["fencing", "Fencing"],
  ["aggregate", "Aggregate"],
  ["concrete", "Concrete"],
  ["tools_rentals", "Tools & rentals"],
  ["other", "Other"],
];
const CATEGORY_LABEL = new Map(SUPPLY_CATEGORIES);

export function categoryLabel(key: string): string {
  return CATEGORY_LABEL.get(key) ?? key;
}

/** ITS_Vendors Region PICKLIST vocabulary — the vendor-picker filter chip axis. */
export const REGIONS = ["West", "Midwest", "East", "National"];

// ── Money display + parse (integer math only — no floats in the money path) ─────────────────────

/** Integer cents → "$1,234.56". Pure integer math (no float division) so display can never
 *  drift from the cents the Worker signs. */
export function formatCents(cents: number): string {
  const sign = cents < 0 ? "-" : "";
  const abs = Math.abs(cents);
  const dollars = Math.floor(abs / 100);
  const frac = String(abs % 100).padStart(2, "0");
  return `${sign}$${dollars.toLocaleString("en-US")}.${frac}`;
}

/** "1,234.56" / "$1,234.56" → 123456 integer cents; null on anything unparseable or >2dp.
 *  String math (never parseFloat×100 — 19.99*100 !== 1999). */
export function parseDollarsToCents(input: string): number | null {
  const t = input.trim().replace(/^\$/, "").replace(/,/g, "");
  if (!/^\d+(\.\d{0,2})?$/.test(t)) return null;
  const [d, f = ""] = t.split(".");
  const cents = parseInt(d, 10) * 100 + (f ? parseInt(f.padEnd(2, "0"), 10) : 0);
  return Number.isSafeInteger(cents) ? cents : null;
}

/** "$0.35" per-watt dollars → 35_000_000 microcents (1 microcent = 1e-6 cents; dollars×1e8).
 *  Up to 8 decimal places; string math, same rationale as parseDollarsToCents. */
export function parseDollarsToMicrocents(input: string): number | null {
  const t = input.trim().replace(/^\$/, "").replace(/,/g, "");
  if (!/^\d+(\.\d{0,8})?$/.test(t)) return null;
  const [d, f = ""] = t.split(".");
  const micro = parseInt(d, 10) * 100_000_000 + (f ? parseInt(f.padEnd(8, "0"), 10) : 0);
  return Number.isSafeInteger(micro) ? micro : null;
}

/** DISPLAY mirror of worker/po.ts lineExtendedCents — byte-identical rounding: per-watt lines
 *  (watts + ppw present) use round(watts × ppw_microcents / 1e6); everything else
 *  round(qty × unit_cost_cents). */
export function lineExtendedCents(l: {
  qty: number;
  unit_cost_cents: number | null;
  watts: number | null;
  price_per_watt_microcents: number | null;
}): number {
  if (l.watts !== null && l.price_per_watt_microcents !== null) {
    return Math.round((l.watts * l.price_per_watt_microcents) / 1_000_000);
  }
  return Math.round(l.qty * (l.unit_cost_cents ?? 0));
}

/** DISPLAY mirror of worker/po.ts computeTotals. `ratesBp` comes from GET /api/po/config
 *  (the same tax.json vocabulary the Worker imports at build time). Returns null when the tax
 *  basis is unresolvable — tax_mode 'auto' with a state missing from the table FAILS CLOSED
 *  exactly like the Worker (a silent 0% would understate tax on a legal document). */
export function computeDisplayTotals(
  lines: { qty: number; unit_cost_cents: number | null; watts: number | null; price_per_watt_microcents: number | null }[],
  taxMode: TaxMode,
  taxRateBpOverride: number,
  shippingCents: number,
  shipToState: string,
  ratesBp: Record<string, number>,
): PoTotals | null {
  let subtotal = 0;
  for (const l of lines) subtotal += lineExtendedCents(l);
  let rate = 0;
  if (taxMode === "auto") {
    const t = ratesBp[shipToState];
    if (t === undefined) return null;
    rate = t;
  } else if (taxMode === "override") {
    rate = taxRateBpOverride;
  } // exempt / included → 0
  const tax = Math.round((subtotal * rate) / 10_000);
  return { subtotal_cents: subtotal, tax_rate_bp: rate, tax_cents: tax, total_cents: subtotal + tax + shippingCents };
}
