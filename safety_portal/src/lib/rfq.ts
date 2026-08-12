// RFQ API client (ADR-0004 R1, po_materials sub-lane). Same-origin cookie fetch; every
// route is session + cap.po.manage gated server-side (worker/rfq.ts) — the SPA gating is
// convenience, never the boundary (Invariant 2).
//
// PRICE-FREE BY DESIGN: an RFQ asks vendors for prices, it never carries one. There are no
// money fields anywhere on this surface — dollars enter the system only through the
// vendor-estimate importer + the human disposition (src/lib/estimates.ts → the EXISTING
// PO draft validators). The Worker signs the rfq:v1 canonical at generate; sending happens
// Mac-side only after F22-verified human approval (Invariant 1 — nothing here transmits).

import { ApiError, raiseApiError } from "./errorCopy";

// ── Wire shapes (mirror worker/rfq.ts) ──────────────────────────────────────────────────────────

export type RfqStatus =
  | "draft"
  | "queued"
  | "generated"
  | "partially_sent"
  | "sent"
  | "closed"
  | "canceled";

export type RfqVendorStatus = "pending" | "filed" | "sent" | "responded" | "canceled";

/** One addressed vendor's row (the per-vendor fan-out unit — badge material). */
export interface RfqVendorRow {
  id: number;
  vendor_key: string;
  status: RfqVendorStatus;
  box_pdf_file_id: string | null;
  box_form_file_id: string | null;
  review_row_id: string | null;
  responded_estimate_id: number | null;
  sent_at: number | null;
}

/** One price-free line item. */
export interface RfqLine {
  position: number;
  part_number: string;
  description: string;
  qty: number | null;
  unit: string;
  line_note: string;
}

/** One row of GET /api/po/rfqs — the tracker list (vendor rows ride along for badges). */
export interface RfqListRow {
  id: number;
  rfq_number: string | null;
  job_no: string;
  /** 0070 — the Evergreen identifier's site segment; 0 = no site. The RFQ number
   *  carries it as `RFQ-{job_no}.{site}-{NNN}` so two sites of one project do not
   *  share a sequence. */
  site_phase: number;
  job_name: string;
  ship_to_name: string;
  ship_to_address: string;
  ship_to_city: string;
  ship_to_state: string;
  ship_to_zip: string;
  delivery_contact_name: string;
  delivery_contact_phone: string;
  delivery_contact_email: string;
  scope_text: string;
  due_date: string | null;
  status: RfqStatus;
  draft_version: number;
  created_by: string;
  created_at: number;
  updated_at: number;
  vendors: RfqVendorRow[];
}

/** GET /api/po/rfqs/:id — the builder/detail read. */
export interface RfqDetail {
  rfq: Omit<RfqListRow, "vendors">;
  line_items: RfqLine[];
  vendors: RfqVendorRow[];
}

/** The writable draft body (create + update share it; worker parseRfqDraftBody). Line
 *  positions are server-assigned from array order. */
export interface RfqDraftBody {
  job_no: string;
  /** 0070 — the site segment. Omit or 0 for a job with no site breakdown. */
  site_phase?: number;
  job_name?: string;
  ship_to_name?: string;
  ship_to_address?: string;
  ship_to_city?: string;
  ship_to_state?: string;
  ship_to_zip?: string;
  delivery_contact_name?: string;
  delivery_contact_phone?: string;
  delivery_contact_email?: string;
  scope_text?: string;
  due_date?: string | null;
  line_items: { part_number?: string; description: string; qty?: number | null; unit?: string; line_note?: string }[];
  vendor_keys: string[];
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

export async function fetchRfqs(status?: RfqStatus): Promise<RfqListRow[]> {
  const q = status ? `?status=${encodeURIComponent(status)}` : "";
  const data = await getJson<{ rfqs: RfqListRow[] }>(`/api/po/rfqs${q}`);
  return data.rfqs ?? [];
}

export async function fetchRfq(id: number): Promise<RfqDetail> {
  return getJson<RfqDetail>(`/api/po/rfqs/${id}`);
}

export async function createRfqDraft(body: RfqDraftBody): Promise<{ id: number }> {
  return postJson<{ ok: boolean; id: number }>("/api/po/rfqs", body);
}

export async function updateRfqDraft(id: number, body: RfqDraftBody): Promise<void> {
  await postJson(`/api/po/rfqs/${id}/update`, body);
}

/** The generate outcomes the builder branches on. `draft_changed` = a concurrent edit
 *  landed inside generate's window — refetch and regenerate; `rfq_number_conflict` = a
 *  lost allocation race — simply retry; `not_draft` = someone else already generated. */
export type RfqGenerateResult =
  | { ok: true; id: number; rfq_number: string }
  | { ok: false; error: "draft_changed" | "rfq_number_conflict" | "not_draft" };

export async function generateRfq(id: number): Promise<RfqGenerateResult> {
  const res = await fetch(`/api/po/rfqs/${id}/generate`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({}),
  });
  if (res.ok) {
    const data = (await res.json()) as { id: number; rfq_number: string };
    return { ok: true, ...data };
  }
  const body = (await res.json().catch(() => ({}))) as { error?: string };
  if (body.error === "draft_changed" || body.error === "rfq_number_conflict" || body.error === "not_draft") {
    return { ok: false, error: body.error };
  }
  throw new ApiError(body.error ?? null, res.status);
}

export async function cancelRfq(id: number): Promise<void> {
  await postJson(`/api/po/rfqs/${id}/cancel`, {});
}

// ── Display copy ────────────────────────────────────────────────────────────────────────────────

export const RFQ_STATUS_LABEL: Record<RfqStatus, string> = {
  draft: "Draft",
  queued: "Queued",
  generated: "Generated",
  partially_sent: "Partially sent",
  sent: "Sent",
  closed: "Closed",
  canceled: "Canceled",
};

export const RFQ_VENDOR_STATUS_LABEL: Record<RfqVendorStatus, string> = {
  pending: "Pending",
  filed: "Filed",
  sent: "Sent",
  responded: "Responded",
  canceled: "Canceled",
};

/** Worker-mirrored bounds (the Worker re-gates every one — Invariant 2). */
export const MAX_RFQ_VENDORS = 12;
export const MAX_RFQ_LINES = 100;
