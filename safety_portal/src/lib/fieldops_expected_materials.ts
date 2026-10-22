// Expected-materials API client (Material receipts M1). Same-origin cookie fetch; no auth header.
// READ + receive/flag-incident gated cap.materials.receive (per-job ownership-scoped for
// non-admins); expectation CRUD gated cap.materials.manage (the Worker re-gates every call).
// receive/flagIncident ship here for completeness — M2 wires them into the daily form's
// deliveries region ("Confirm receipt" / "Report material incident →").

import type { ExpectedMaterialsResponse } from "../../worker/wire-types";

// Wire shapes — SINGLE-SOURCED in worker/wire-types.ts (the Worker types its c.json payload with
// the same definitions, so a shape drift fails the typecheck on both sides); re-exported here so
// existing importers keep their path.
export type {
  ExpectedMaterialRow,
  ExpectedMaterialStatus,
  MaterialReceiptEventRow,
  MaterialReceiptKind,
  MaterialShipmentRow,
} from "../../worker/wire-types";
import type { ExpectedMaterialStatus, MaterialReceiptKind } from "../../worker/wire-types";

export async function fetchExpectedMaterials(jobId: string): Promise<ExpectedMaterialsResponse> {
  const res = await fetch(`/api/fieldops/expected-materials?job_id=${encodeURIComponent(jobId)}`, {
    credentials: "same-origin",
  });
  if (!res.ok) throw new Error("Could not load this job's expected materials.");
  return ((await res.json()) as ExpectedMaterialsResponse) ?? { expected_materials: [] };
}

async function postJson<T = { ok: boolean }>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = (await res.json().catch(() => ({}))) as { error?: string };
    throw new Error(err.error ?? `Request failed (${res.status})`);
  }
  return (await res.json()) as T;
}

/** Content fields for add/edit. Catalog-pick rows set material_id (description optional extra);
 *  free-text rows omit material_id and REQUIRE description (the Worker 400s otherwise). */
export interface ExpectedMaterialFields {
  material_id?: number;
  description?: string;
  qty?: number;
  unit?: string;
  /** The expected DELIVERY date (the column's 0031 meaning; the UI labels it so). */
  expected_date?: string;
  // ── Materials tracking (PR2, 0059) ──────────────────────────────────────────────────────────
  part_number?: string;
  category?: string;
  expected_ship_date?: string;
}

/** Fields for a scheduled load attached to a line. */
export interface MaterialShipmentFields {
  part_number?: string;
  bol_number?: string;
  carrier?: string;
  qty?: number;
  unit?: string;
  ship_date?: string;
  delivery_date?: string;
  seq?: number;
}

export async function createExpectedMaterial(
  jobId: string,
  fields: ExpectedMaterialFields & { seq?: number },
): Promise<{ id: number }> {
  return postJson<{ ok: boolean; id: number }>("/api/fieldops/expected-material", { job_id: jobId, ...fields });
}

/** Full-replace edit of the content fields. Only status='expected' rows are editable —
 *  a received/incident row is a receipt record (the Worker 409s not_editable). */
export async function updateExpectedMaterial(id: number, fields: ExpectedMaterialFields): Promise<void> {
  await postJson(`/api/fieldops/expected-material/${id}/update`, fields);
}

/** Reorder: seq-only write, allowed on any active row (the checklist planRenumber convention). */
export async function setExpectedMaterialSeq(id: number, seq: number): Promise<void> {
  await postJson(`/api/fieldops/expected-material/${id}/seq`, { seq });
}

/** Deactivate (soft-delete; idempotent — history kept). */
export async function deactivateExpectedMaterial(id: number): Promise<void> {
  await postJson(`/api/fieldops/expected-material/${id}/delete`, {});
}

/** Confirm receipt — a 'delivered' mark. Under 0059 it appends to the receipt ledger, so a REPEAT
 *  is a legal second event, not a 409. It has NO SPA caller: the daily form's "Confirm receipt"
 *  button it served was removed with the deep-link card (#74, 2026-08-11), leaving the Materials
 *  page's `markReceipt` as the only mark surface. Any caller re-added here must ask the D9
 *  question first (lib/materials_view.needsSettledConfirm) — that rule lives in one place. */
export async function receiveExpectedMaterial(
  id: number,
  opts: { qty_received?: number; note?: string } = {},
): Promise<void> {
  await postJson(`/api/fieldops/expected-material/${id}/receive`, opts);
}

/** The three-way delivery mark (PR2). Appends a receipt event; repeatable by design, because a
 *  part number routinely arrives across several loads. `qty` is REQUIRED on delivered and
 *  partial (400 `qty_required` otherwise — a mark that does not say how much arrived cannot move
 *  the outstanding balance), and forbidden on not_delivered, where `note` is required instead.
 *  JobMaterialsPage blocks all three client-side before posting. */
export async function markReceipt(
  id: number,
  body: {
    kind: MaterialReceiptKind;
    qty?: number;
    note?: string;
    event_date?: string;
    shipment_id?: number;
    /** A CORRECTION (0080, decision D3): the VOIDED event on this same line that this mark
     *  replaces. The Worker refuses one that is not voided (409 supersedes_not_voided), not on
     *  this line (422 unknown_supersedes), or already corrected (409 already_corrected), and
     *  files the correction in the original's history position however much later it lands (D4). */
    supersedes_id?: number;
  },
): Promise<{ event_id: number | null }> {
  return postJson<{ ok: boolean; id: number; event_id: number | null; kind: MaterialReceiptKind }>(
    `/api/fieldops/expected-material/${id}/receipt`,
    body,
  );
}

/** VOID a delivery mark (0080). The row is KEPT — annotated with who/when/why and excluded from
 *  every rollup — never deleted: the ledger stays append-only, and the void is itself an audited
 *  event. `reason` is REQUIRED (1..500 chars after trim; 400 reason_required / invalid_reason).
 *  The same people who may mark may void (decision D1), and the Worker re-gates it: role, job
 *  scope, an ACTIVE job (409 job_not_active), and one void per mark (409 already_voided).
 *  Returns the line's persisted status + received total after the void. */
export async function voidReceiptEvent(
  eventId: number,
  reason: string,
): Promise<{
  ok: boolean;
  event_id: number;
  line_id: number;
  status: ExpectedMaterialStatus | null;
  qty_received: number | null;
}> {
  return postJson(`/api/fieldops/receipt-event/${eventId}/void`, { reason });
}

export async function createMaterialShipment(
  lineId: number,
  fields: MaterialShipmentFields,
): Promise<{ id: number }> {
  return postJson<{ ok: boolean; id: number }>("/api/fieldops/material-shipment", {
    line_id: lineId,
    ...fields,
  });
}

export async function updateMaterialShipment(id: number, fields: MaterialShipmentFields): Promise<void> {
  await postJson(`/api/fieldops/material-shipment/${id}/update`, fields);
}

export async function deactivateMaterialShipment(id: number): Promise<void> {
  await postJson(`/api/fieldops/material-shipment/${id}/delete`, {});
}

/** Flag a delivery problem (expected → incident). note is REQUIRED (the Worker 400s without it). */
export async function flagExpectedMaterialIncident(
  id: number,
  note: string,
  qtyReceived?: number,
): Promise<void> {
  await postJson(`/api/fieldops/expected-material/${id}/flag-incident`, {
    note,
    ...(qtyReceived !== undefined ? { qty_received: qtyReceived } : {}),
  });
}

/** Clear a line's problem flag — an explicit human act, never a side effect of a delivery
 *  mark (a delivered-but-damaged pallet is still a problem). note is REQUIRED: the record
 *  of HOW it was resolved is the point. Status lands where the delivery ledger says —
 *  latest event delivered → received, otherwise back to expected. */
export async function resolveExpectedMaterialIncident(
  id: number,
  note: string,
): Promise<{ status: string | null }> {
  return postJson<{ ok: boolean; id: number; status: string | null }>(
    `/api/fieldops/expected-material/${id}/resolve-incident`,
    { note },
  );
}
