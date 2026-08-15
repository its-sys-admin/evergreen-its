// Subcontract API client (SC-S5). Same-origin cookie fetch; every route is session +
// cap.subcontracts.manage gated server-side (worker/subcontract.ts) — SPA gating is convenience,
// never the boundary (Invariant 2). Mirror of src/lib/po.ts, with the subcontract deltas:
// region→state, supply_categories→trades, +coi_reference/license_number, NO tax.
//
// MONEY IS INTEGER CENTS. A subcontract is a LUMP-SUM contract price: NO tax, NO shipping, NO
// per-watt line. subtotal_cents = Σ sov_lines.extended_cents and MUST equal contract_price_cents
// (the SOV-sums-to-price gate). The Worker recomputes all money server-side; generate hard-409s
// (`sov_mismatch`, carrying the integer `recomputed`) when the client's displayed contract price
// disagrees. The Worker's numbers are always authoritative; the display mirror exists only so the
// admin watches the live subtotal while typing, never so the client is trusted.

import { ApiError, raiseApiError } from "./errorCopy";
// formatCents + parseDollarsToCents are IDENTICAL to PO's integer-cents helpers — re-export them
// (do NOT duplicate: money format lives in one place, HOUSE_REFLEXES §1 multi-surface fan-out).
export { formatCents, parseDollarsToCents } from "./po";

// ── Wire shapes (mirror worker/subcontract.ts) ─────────────────────────────────────────────────────

/** GET /api/subcontracts/subcontractors row — the §51 cache of the ITS_Subcontractors SoR.
 *  `trades` arrives JSON-parsed to string[] (worker parseJsonArray). */
export interface Subcontractor {
  sub_key: string;
  sub_name: string;
  address: string;
  contact_name: string;
  contact_email: string;
  contact_phone: string;
  state: string;
  trades: string[];
  default_terms_profile: string;
  msa_reference: string;
  coi_reference: string;
  license_number: string;
  active: number;
  notes: string;
  origin: string;
  /** §51 up-sync state: 'pending' = a portal edit not yet mirrored to ITS_Subcontractors. */
  sync_state: string;
  mirror_version: number;
}

/** The writable subcontractor fields (create + update share the shape; worker parseSubcontractorFields).
 *  Deactivation rides `active: 0` — NEVER a delete (D4). */
export interface SubcontractorFields {
  sub_name: string;
  address?: string;
  contact_name?: string;
  contact_email?: string;
  contact_phone?: string;
  state?: string;
  trades?: string[];
  default_terms_profile?: string;
  msa_reference?: string;
  coi_reference?: string;
  license_number?: string;
  notes?: string;
  /** 1 = live, 0 = deactivated (never a delete — D4). Omitted = keep-live. */
  active?: number;
}

/** One Schedule-of-Values line as served (position + extended are SERVER-assigned). */
export interface SovLine {
  position: number;
  item_number: string;
  description: string;
  qty: number;
  unit: string;
  unit_price_cents: number | null;
  extended_cents: number;
}

/** One client SOV line in a draft body (position + extended are SERVER-assigned/ignored).
 *  unit_price_cents is REQUIRED on every line (worker decision #7 — all money server-derived). */
export interface SovDraftLine {
  item_number?: string;
  description: string;
  qty: number;
  unit?: string;
  unit_price_cents: number;
}

export type PriceBasis = "fixed" | "not_to_exceed";
export type TemplateFamily = "long_form" | "short_form";

export type SubcontractStatus =
  | "draft"
  | "queued"
  | "pending_review"
  | "approved"
  | "sent"
  | "executed"
  | "superseded"
  | "canceled";

/** One row of GET /api/subcontracts/subs — the tracker list projection. */
export interface SubcontractListRow {
  id: number;
  sc_number: string | null;
  job_no: string;
  site_phase: number;
  supersede_seq: number;
  revision: number | null;
  sub_key: string;
  job_id: string;
  job_name: string;
  project_name: string;
  owner_entity: string;
  status: SubcontractStatus;
  contract_price_cents: number;
  supersedes_sc_id: number | null;
  /** Track D2: set when this subcontract IS a change order against `change_order_of`; its
   *  number is `{parent number}-CO{co_seq}`, minted at generate. The parent stays in force. */
  change_order_of: number | null;
  co_seq: number | null;
  box_file_id: string | null;
  created_by: string;
  created_at: number;
  updated_at: number;
}

/** GET /api/subcontracts/subs/:id serves the FULL row (SELECT *) — the draft-edit / preview read. */
export interface SubcontractDetail extends SubcontractListRow {
  prime_contractor: string;
  site_name: string;
  site_address: string;
  governing_law_state: string;
  trade: string;
  exhibit_a_template_id: string;
  exhibit_a_template_version: string;
  exhibit_a_work_text: string;
  scope_summary: string;
  price_basis: string;
  retainage_bp: number;
  subtotal_cents: number;
  start_date: string;
  completion_date: string;
  terms_profile_id: string;
  terms_version: string;
  template_family: string;
  approver_name: string;
  approver_title: string;
}

/** POST /api/subcontracts/drafts (+ /:id/update) body — mirrors worker parseDraftBody. NO tax /
 *  shipping / per-watt. `sov_lines` MAY be omitted/empty → the Worker auto-derives a single line
 *  {qty:1, unit_price_cents: contract_price_cents}. */
export interface DraftBody {
  sub_key: string;
  job_no: string;
  site_phase: number;
  job_id?: string;
  job_name?: string;
  project_name?: string;
  owner_entity?: string;
  prime_contractor?: string;
  site_name?: string;
  site_address?: string;
  governing_law_state?: string;
  trade?: string;
  exhibit_a_template_id?: string;
  exhibit_a_template_version?: string;
  exhibit_a_work_text?: string;
  scope_summary?: string;
  price_basis?: PriceBasis;
  /** REQUIRED — the lump-sum contract price (integer cents). */
  contract_price_cents: number;
  retainage_bp?: number;
  start_date?: string;
  completion_date?: string;
  terms_profile_id?: string;
  terms_version?: string;
  template_family?: TemplateFamily;
  approver_name?: string;
  approver_title?: string;
  sov_lines?: SovDraftLine[];
}

/** GET /api/subcontracts/terms. `render_line` only on attach-kind profiles. */
export interface TermsProfile {
  id: string;
  kind: "library" | "attach";
  label: string;
  description: string;
  current_version: string | null;
  tokens: string[];
  render_line: string | null;
}

/** GET /api/subcontracts/terms/:id/text — a library profile's CURRENT version clause body
 *  (header-stripped), for an editor's edit-text pre-fill. Attach/unknown profiles 404. */
export interface TermsText {
  profile_id: string;
  version: string;
  text: string;
}

/** GET /api/subcontracts/terms/:id/versions — the version list (id + legal_review). */
export interface TermsVersionRow {
  version: string;
  legal_review: string;
}
export interface TermsVersions {
  profile_id: string;
  current_version: string | null;
  versions: TermsVersionRow[];
}

/** GET /api/subcontracts/exhibit-templates?trade= — the Exhibit A Article II ("The Work") pre-fill for
 *  a chosen Trade. `template_key` is the manifest body a Trade fans onto (AC/MV/DC Electrical all →
 *  'electrical'); `article_ii` is that trade's standard Art II body, the editable exhibit_a_work_text
 *  starting point. An unknown Trade 400s (invalid_trade → ApiError). */
export interface ExhibitTemplate {
  trade: string;
  template_key: string;
  article_ii: string;
}

/** GET /api/subcontracts/config — versioned contractor identity + §2.5 payment-terms defaults +
 *  the governing-law state list. NO tax table (subcontracts have no tax). */
export interface SubcontractConfig {
  contractor: {
    entity: string;
    address_lines: string[];
    phone: string;
    signature_entity: string;
    prime_contractor_default: string;
  };
  payment_terms: {
    retainage_bp: number;
    retainage_reduced_bp: number;
    retainage_reduction_at_pct: number;
  };
  /** 2-letter USPS codes only (no names — pair with US_STATES / stateName for display). */
  governing_law_states: string[];
}

// ── Fetch helpers (module-private, byte-copied from po.ts) ──────────────────────────────────────────

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

// ── Subcontractors ─────────────────────────────────────────────────────────────────────────────────

export async function fetchSubcontractors(includeInactive = false): Promise<Subcontractor[]> {
  const q = includeInactive ? "?include_inactive=1" : "";
  const data = await getJson<{ subcontractors: Subcontractor[] }>(`/api/subcontracts/subcontractors${q}`);
  return data.subcontractors ?? [];
}

export async function createSubcontractor(fields: SubcontractorFields): Promise<{ ok: boolean; sub_key: string }> {
  return postJson<{ ok: boolean; sub_key: string }>("/api/subcontracts/subcontractors", fields);
}

export async function updateSubcontractor(subKey: string, fields: SubcontractorFields): Promise<void> {
  await postJson(`/api/subcontracts/subcontractors/${encodeURIComponent(subKey)}/update`, fields);
}

// ── Subcontracts (tracker list + detail) ─────────────────────────────────────────────────────────────

export async function fetchSubcontracts(status?: SubcontractStatus): Promise<SubcontractListRow[]> {
  const q = status ? `?status=${encodeURIComponent(status)}` : "";
  const data = await getJson<{ subcontracts: SubcontractListRow[] }>(`/api/subcontracts/subs${q}`);
  return data.subcontracts ?? [];
}

export async function fetchSubcontract(
  id: number,
): Promise<{ subcontract: SubcontractDetail; sov_lines: SovLine[] }> {
  return getJson(`/api/subcontracts/subs/${id}`);
}

// ── Drafts (create / update / generate) ──────────────────────────────────────────────────────────────

export async function createDraft(body: DraftBody): Promise<{ id: number; subtotal_cents: number }> {
  return postJson<{ ok: boolean; id: number; subtotal_cents: number }>("/api/subcontracts/drafts", body);
}

export async function updateDraft(id: number, body: DraftBody): Promise<{ id: number; subtotal_cents: number }> {
  return postJson<{ ok: boolean; id: number; subtotal_cents: number }>(`/api/subcontracts/drafts/${id}/update`, body);
}

/** The generate outcomes the builder branches on. `sov_mismatch` carries the server's `recomputed`
 *  integers — the SPA re-renders FROM those (the Worker is authoritative) and generates again.
 *  `draft_changed` / `sc_number_conflict` / `not_draft` are the other contractual 409s (worker/
 *  subcontract.ts generate); anything else (no_sov_lines, invalid_governing_law_state,
 *  subtotal_overflow, …) throws ApiError. */
export type GenerateResult =
  | { ok: true; id: number; sc_number: string; revision: number; subtotal_cents: number }
  | { ok: false; error: "sov_mismatch"; recomputed: { subtotal_cents: number; contract_price_cents: number } }
  | { ok: false; error: "draft_changed" | "sc_number_conflict" | "not_draft" };

export async function generateDraft(
  id: number,
  displayed: { contract_price_cents: number },
): Promise<GenerateResult> {
  const res = await fetch(`/api/subcontracts/drafts/${id}/generate`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ contract_price_cents: displayed.contract_price_cents }),
  });
  if (res.ok) {
    const data = (await res.json()) as { id: number; sc_number: string; revision: number; subtotal_cents: number };
    return { ok: true, ...data };
  }
  const body = (await res.json().catch(() => ({}))) as {
    error?: string;
    recomputed?: { subtotal_cents: number; contract_price_cents: number };
  };
  if (body.error === "sov_mismatch" && body.recomputed) {
    return { ok: false, error: "sov_mismatch", recomputed: body.recomputed };
  }
  if (body.error === "draft_changed" || body.error === "sc_number_conflict" || body.error === "not_draft") {
    return { ok: false, error: body.error };
  }
  throw new ApiError(body.error ?? null, res.status);
}

/** Supersede a SENT or EXECUTED subcontract: the Worker clones it into a new draft (supersede_seq+1).
 *  A live successor already in flight comes back as `supersede_in_progress` + its id — the SPA opens
 *  THAT draft instead of minting a sibling. Success carries `supersedes_sc_id` (the source). */
export type SupersedeResult =
  | { ok: true; id: number; supersedes_sc_id: number }
  | { ok: false; error: "supersede_in_progress"; existing_id: number };

export async function supersedeSubcontract(id: number): Promise<SupersedeResult> {
  const res = await fetch(`/api/subcontracts/${id}/supersede`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({}),
  });
  if (res.ok) {
    const data = (await res.json()) as { id: number; supersedes_sc_id: number };
    return { ok: true, id: data.id, supersedes_sc_id: data.supersedes_sc_id };
  }
  const body = (await res.json().catch(() => ({}))) as { error?: string; existing_id?: number };
  if (body.error === "supersede_in_progress" && typeof body.existing_id === "number") {
    return { ok: false, error: "supersede_in_progress", existing_id: body.existing_id };
  }
  throw new ApiError(body.error ?? null, res.status);
}

/** Create a CHANGE-ORDER draft against a SENT or EXECUTED subcontract (Track D2): the Worker
 *  clones the parent's full configuration + SOV into a fresh draft linked change_order_of/co_seq.
 *  The parent stays in force; the CO's number (`{parent}-CO{n}`) is minted at its own generate.
 *  Failures (wrong state, CO-of-CO, a lost co_seq race) throw ApiError for errorCopy translation. */
export async function createSubChangeOrder(id: number): Promise<{ id: number; co_seq: number | null }> {
  const res = await fetch(`/api/subcontracts/${id}/change-order`, {
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

export async function cancelSubcontract(id: number): Promise<void> {
  await postJson(`/api/subcontracts/${id}/cancel`, {});
}

/** HARD-delete an un-generated DRAFT (row + SOV lines). Draft-only; a generated record is 409 not_deletable. */
export async function deleteSubDraft(id: number): Promise<void> {
  await postJson(`/api/subcontracts/${id}/delete`, {});
}

// ── Terms + config ───────────────────────────────────────────────────────────────────────────────────

export async function fetchTerms(): Promise<TermsProfile[]> {
  const data = await getJson<{ profiles: TermsProfile[] }>("/api/subcontracts/terms");
  return data.profiles ?? [];
}

export async function fetchTermsText(profileId: string): Promise<TermsText> {
  return getJson<TermsText>(`/api/subcontracts/terms/${encodeURIComponent(profileId)}/text`);
}

export async function fetchTermsVersions(profileId: string): Promise<TermsVersions> {
  return getJson<TermsVersions>(`/api/subcontracts/terms/${encodeURIComponent(profileId)}/versions`);
}

export async function fetchSubcontractConfig(): Promise<SubcontractConfig> {
  return getJson<SubcontractConfig>("/api/subcontracts/config");
}

/** Pre-fill Article II ("The Work") for the operator-picked Trade. Resolves the Trade → its art2
 *  template body server-side; an unknown Trade throws ApiError('invalid_trade', 400). */
export async function fetchExhibitTemplate(trade: string): Promise<ExhibitTemplate> {
  return getJson<ExhibitTemplate>(`/api/subcontracts/exhibit-templates?trade=${encodeURIComponent(trade)}`);
}

/** Site-address auto-fill for the operator-picked job (C1). Reads the Smartsheet ITS_Active_Jobs
 *  "Address" the Worker syncs down (jobs.address). A 404 / blank leaves the field operator-editable. */
export interface JobSiteAddress {
  job_id: string;
  site_address: string;
}
export async function fetchJobSiteAddress(jobId: string): Promise<JobSiteAddress> {
  return getJson<JobSiteAddress>(`/api/subcontracts/jobs/${encodeURIComponent(jobId)}/site-address`);
}

// ── Exhibit A per-trade Article II templates — the config editor's read surface (PR-B2) ────────────
export interface ExhibitVersionRow {
  version: string;
  legal_review: string;
}
export interface ExhibitTemplateSummary {
  template_key: string;
  current_version: string;
  trades: string[];
  versions: ExhibitVersionRow[];
}
export interface ExhibitKeyText {
  template_key: string;
  version: string;
  article_ii: string;
}
export interface ExhibitKeyVersions {
  template_key: string;
  current_version: string;
  versions: ExhibitVersionRow[];
}
/** Every trade-template key + its current_version, versions (legal_review), and mapped Trades. */
export async function fetchExhibitTemplateKeys(): Promise<ExhibitTemplateSummary[]> {
  const data = await getJson<{ templates: ExhibitTemplateSummary[] }>("/api/subcontracts/exhibit-keys");
  return data.templates;
}
/** A key's Article II body at its current (or an explicit) version — the 'edit from live' pre-fill. */
export async function fetchExhibitKeyText(key: string, version?: string): Promise<ExhibitKeyText> {
  const q = version ? `?version=${encodeURIComponent(version)}` : "";
  return getJson<ExhibitKeyText>(`/api/subcontracts/exhibit-keys/${encodeURIComponent(key)}/text${q}`);
}
/** A key's versions + current_version (the make-current picker). */
export async function fetchExhibitKeyVersions(key: string): Promise<ExhibitKeyVersions> {
  return getJson<ExhibitKeyVersions>(`/api/subcontracts/exhibit-keys/${encodeURIComponent(key)}/versions`);
}
/** The live Trade vocabulary — the manifest trade_map keys, served by the Worker. The single source of
 *  truth so a trade added via the config editor (exhibit create_profile) appears in the pickers with no
 *  code edit. Callers fall back to the static {@link TRADES} on a degraded fetch (never an empty picker). */
export async function fetchTrades(): Promise<string[]> {
  const data = await getJson<{ trades: string[] }>("/api/subcontracts/trades");
  return data.trades;
}

// ── Surface-line name aliases ──────────────────────────────────────────────────────────────────────
// The S5 build spec (lib_routing.md) names these fetchSubcontracts / fetchSubcontract / createDraft /
// updateDraft / generateDraft / fetchTerms / fetchSubcontractConfig; the task surface line names them
// fetchSubDrafts / fetchSubDraft / createSubDraft / updateSubDraft / generateSubcontract / fetchSubTerms
// / fetchSubConfig. Export BOTH so either page-agent import resolves — one implementation, no drift.
export {
  fetchSubcontracts as fetchSubDrafts,
  fetchSubcontract as fetchSubDraft,
  createDraft as createSubDraft,
  updateDraft as updateSubDraft,
  generateDraft as generateSubcontract,
  fetchTerms as fetchSubTerms,
  fetchSubcontractConfig as fetchSubConfig,
};

// ── Money display mirror (no tax — the total IS the subtotal) ────────────────────────────────────────

/** DISPLAY mirror of worker/subcontract.ts sovExtendedCents — byte-identical rounding:
 *  round(qty × unit_price_cents). ECMA Math.round is half-up and must bit-agree with money.py. */
export function sovExtendedCents(l: { qty: number; unit_price_cents: number | null }): number {
  return Math.round(l.qty * (l.unit_price_cents ?? 0));
}

/** DISPLAY mirror of worker/subcontract.ts computeSubtotal — Σ extended. There is NO tax/shipping/
 *  total; the total IS the subtotal, and the generate gate is `subtotal === contract_price_cents`. */
export function computeSubtotal(lines: { qty: number; unit_price_cents: number | null }[]): number {
  let subtotal = 0;
  for (const l of lines) subtotal += sovExtendedCents(l);
  return subtotal;
}

// ── Vocabulary constants ─────────────────────────────────────────────────────────────────────────────

/** STATIC FALLBACK for the trade pickers — used only when {@link fetchTrades} degrades (a network/
 *  auth blip), so the picker is never empty. The LIVE source of truth is the manifest trade_map, served
 *  by GET /api/subcontracts/trades and derived server-side (`_SUBCONTRACTOR_TRADE_VALUES`, also manifest-
 *  derived). The Worker treats `trades` as free-form strings, BUT the §51 up-sync writes them to
 *  ITS_Subcontractors, whose Trades picklist is gated by that REGISTRY — a trade outside the manifest set
 *  fences the up-sync (PicklistViolationError). This literal mirrors the manifest's baseline trades; a
 *  trade added later via the config editor appears via fetchTrades, not here. */
export const TRADES: string[] = [
  "Surveying",
  "Civil",
  "Fencing",
  "Post Installation",
  "Mechanical",
  "AC Electrical",
  "MV Electrical",
  "DC Electrical",
  "Specialty",
];

/** The 50 states + DC — [2-letter USPS code, display name]. Mirrors the Worker's
 *  GOVERNING_LAW_STATE_CODES (governing_law.py fail-closed set); config serves codes only, so this
 *  supplies the names for the <select> and the state-group headers on the directory page. */
export const US_STATES: [string, string][] = [
  ["AL", "Alabama"],
  ["AK", "Alaska"],
  ["AZ", "Arizona"],
  ["AR", "Arkansas"],
  ["CA", "California"],
  ["CO", "Colorado"],
  ["CT", "Connecticut"],
  ["DE", "Delaware"],
  ["FL", "Florida"],
  ["GA", "Georgia"],
  ["HI", "Hawaii"],
  ["ID", "Idaho"],
  ["IL", "Illinois"],
  ["IN", "Indiana"],
  ["IA", "Iowa"],
  ["KS", "Kansas"],
  ["KY", "Kentucky"],
  ["LA", "Louisiana"],
  ["ME", "Maine"],
  ["MD", "Maryland"],
  ["MA", "Massachusetts"],
  ["MI", "Michigan"],
  ["MN", "Minnesota"],
  ["MS", "Mississippi"],
  ["MO", "Missouri"],
  ["MT", "Montana"],
  ["NE", "Nebraska"],
  ["NV", "Nevada"],
  ["NH", "New Hampshire"],
  ["NJ", "New Jersey"],
  ["NM", "New Mexico"],
  ["NY", "New York"],
  ["NC", "North Carolina"],
  ["ND", "North Dakota"],
  ["OH", "Ohio"],
  ["OK", "Oklahoma"],
  ["OR", "Oregon"],
  ["PA", "Pennsylvania"],
  ["RI", "Rhode Island"],
  ["SC", "South Carolina"],
  ["SD", "South Dakota"],
  ["TN", "Tennessee"],
  ["TX", "Texas"],
  ["UT", "Utah"],
  ["VT", "Vermont"],
  ["VA", "Virginia"],
  ["WA", "Washington"],
  ["WV", "West Virginia"],
  ["WI", "Wisconsin"],
  ["WY", "Wyoming"],
  ["DC", "District of Columbia"],
];
const STATE_NAME = new Map(US_STATES);

/** 2-letter USPS code → display name; the code itself for an unknown/blank value (so an
 *  "Unassigned" bucket or a legacy code still renders a stable header). */
export function stateName(code: string): string {
  return STATE_NAME.get(code) ?? code;
}
