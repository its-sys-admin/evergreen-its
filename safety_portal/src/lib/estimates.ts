// Vendor-estimate importer API client (ADR-0004 E1/E3, po_materials sub-lane). Same-origin
// cookie fetch; every route is session + cap.po.manage gated server-side
// (worker/po_estimates.ts) — the SPA gating is convenience, never the boundary (Invariant 2).
//
// EXTRACTIONS ARE ADVISORY (ADR decision 2): the rows these fetches return came out of an
// UNTRUSTED document. No dollar from here reaches a PO except through the human disposition
// screen and the EXISTING createDraft validators (src/lib/po.ts → POST /api/po/drafts, which
// recomputes all money server-side in integer cents). The single fidelity control is the
// human side-by-side accept against the page previews (ADR decision 3) — which is why the
// disposition page hard-gates accept on a loaded preview.

import { ApiError, raiseApiError } from "./errorCopy";

// ── Wire shapes (mirror worker/po_estimates.ts) ─────────────────────────────────────────────────

export type EstimateStatus =
  | "pending"
  | "claimed"
  | "refused"
  | "needs_review"
  | "extracted"
  | "imported"
  | "rejected"
  | "superseded";

export type EstimateDocType =
  | "quote"
  | "estimate"
  | "proposal"
  | "invoice"
  | "ap_report"
  | "filled_form"
  | "other";

/** One row of GET /api/po/estimates — metadata only (never bytes, never the hmac). */
export interface EstimateRow {
  /** 0069 — the UNAMBIGUOUS job identity ('' when the estimate predates it). job_no alone
   *  cannot resolve the site: 2026.384 is both MH405 (site 1) and OG593 (site 2). */
  job_id: string;
  id: number;
  est_uuid: string;
  job_no: string;
  job_name: string | null;
  vendor_key: string | null;
  filename: string;
  declared_mime: string;
  size_bytes: number;
  sha256: string;
  status: EstimateStatus;
  doc_type: EstimateDocType | null;
  detail: string | null;
  uploaded_by: string;
  box_file_id: string | null;
  family_key: string | null;
  supersedes_estimate_id: number | null;
  po_id: number | null;
  // R4 round-trip auto-bind: when a VERIFIED Tier-0 rfq-form:v1 form round-tripped, the
  // Worker binds these from the daemon's hint (else null). The disposition screen surfaces
  // them as the "auto-bound to RFQ … — confirm vendor" banner + the requested-vs-quoted panel.
  rfq_id: number | null;
  rfq_vendor_key: string | null;
  created_at: number;
  screened_at: number | null;
  extracted_at: number | null;
  disposed_at: number | null;
}

/** The latest live ADVISORY extraction header (payload_json stays server-side). */
export interface EstimateExtraction {
  id: number;
  estimate_id: number;
  tier: number;
  schema_version: string;
  doc_type: string | null;
  vendor_name: string | null;
  quote_number: string | null;
  revision_label: string | null;
  quote_date: string | null;
  valid_until: string | null;
  subtotal_cents: number | null;
  tax_cents: number | null;
  freight_cents: number | null;
  misc_cents: number | null;
  grand_total_cents: number | null;
  math_ok: number;
  confidence: number | null;
  anomalies: string | null;
  created_at: number;
}

/** One ADVISORY extracted line (the disposition grid row). */
export interface ExtractionLine {
  id: number;
  position: number;
  section: string | null;
  part_number: string | null;
  description: string;
  qty: number | null;
  unit: string | null;
  unit_cost_cents: number | null;
  extended_cents: number | null;
  math_ok: number;
  line_note: string | null;
  disposition: "pending" | "accepted" | "rejected" | "edited";
  edited_json: string | null;
}

/** GET /api/po/estimates/:id — the disposition screen read. */
export interface EstimateDetail {
  estimate: EstimateRow;
  extraction: EstimateExtraction | null;
  lines: ExtractionLine[];
  preview_count: number;
}

/** POST /api/po/estimates/:id/dispose body. */
export interface LineDispositionBody {
  line_id: number;
  disposition: "accepted" | "rejected" | "edited";
  edited_json?: string;
}
export interface DisposeBody {
  action: "imported" | "rejected";
  po_id?: number;
  line_dispositions?: LineDispositionBody[];
  /** The "No preview available — I verified against the original document" checkbox.
   *  The Worker enforces the fidelity gate SERVER-SIDE: an import that accepts/edits
   *  extraction lines with zero rendered preview pages AND no acknowledgment is refused
   *  422 preview_evidence_required (the SPA gate is UX, never the boundary). */
  no_preview_verified?: boolean;
}

// ── Fetch helpers (the src/lib/po.ts idioms) ────────────────────────────────────────────────────

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

// ── Routes ──────────────────────────────────────────────────────────────────────────────────────

export async function fetchEstimates(status?: EstimateStatus): Promise<EstimateRow[]> {
  const q = status ? `?status=${encodeURIComponent(status)}` : "";
  const data = await getJson<{ estimates: EstimateRow[] }>(`/api/po/estimates${q}`);
  return data.estimates ?? [];
}

export async function fetchEstimate(id: number): Promise<EstimateDetail> {
  return getJson<EstimateDetail>(`/api/po/estimates/${id}`);
}

/** Upload one office-received vendor estimate (base64 in JSON — the attachment wire).
 *  The Worker bounds-gates (size / filename / MIME allowlist + magic sniff), signs
 *  est:v1, and pools the bytes in D1 for the Mac-side §34 screen + classification;
 *  an exact-byte replay of a live doc comes back 409 `duplicate_estimate`. */
export async function uploadEstimate(args: {
    /** 0069 — carried so the disposition screen can seed the PO's Site/phase. */
    job_id?: string;
  job_no: string;
  job_name?: string;
  vendor_key?: string;
  filename: string;
  mime: string;
  data_b64: string;
}): Promise<{ id: number }> {
  return postJson<{ ok: boolean; id: number }>("/api/po/estimates", args);
}

/** The session preview-image URL (rendered page PNG — never the original bytes). */
export function estimatePreviewUrl(id: number, page: number): string {
  return `/api/po/estimates/${id}/preview/${page}`;
}

/** The dispose outcomes the disposition page branches on. `already_disposed` (409) means a
 *  parallel/earlier call already imported or rejected this estimate — for the import flow the
 *  page treats it as "already imported": discard the just-created duplicate draft + navigate. */
export type DisposeResult =
  | { ok: true; status: "imported" | "rejected" }
  | { ok: false; error: "already_disposed"; status: EstimateStatus | null };

export async function disposeEstimate(id: number, body: DisposeBody): Promise<DisposeResult> {
  const res = await fetch(`/api/po/estimates/${id}/dispose`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.ok) {
    const data = (await res.json()) as { status: "imported" | "rejected" };
    return { ok: true, status: data.status };
  }
  const errBody = (await res.json().catch(() => ({}))) as { error?: string; status?: EstimateStatus };
  if (errBody.error === "already_disposed") {
    return { ok: false, error: "already_disposed", status: errBody.status ?? null };
  }
  throw new ApiError(errBody.error ?? null, res.status);
}

// ── Upload hints (the Worker re-gates every one — Invariant 2) ──────────────────────────────────

export const ESTIMATE_MAX_BYTES = 10_000_000;
export const ESTIMATE_ACCEPT = ".pdf,.jpg,.jpeg,.png,.docx,.xlsx";
export const ESTIMATE_MIME_BY_EXT: Record<string, string> = {
  ".pdf": "application/pdf",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".png": "image/png",
  ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
};

/** Display copy for the tracker's status badges. */
export const ESTIMATE_STATUS_LABEL: Record<EstimateStatus, string> = {
  pending: "Uploaded",
  claimed: "Screening",
  refused: "Refused",
  needs_review: "Needs review",
  extracted: "Extracted",
  imported: "Imported",
  rejected: "Rejected",
  superseded: "Superseded",
};
