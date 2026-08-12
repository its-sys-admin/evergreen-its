// Job-payments API client (ADR-0006 decision 10, PR-7). Same-origin cookie fetch; no
// auth header — this lane has NO bearer tier at all (no daemon reads payments; the
// Worker+SPA are the whole surface).
//
// EVERY route is gated session + cap.payments.manage (admin only — operator decision 4:
// commercially sensitive). The Worker re-gates every call; the SPA's capability check
// drives the Payments card's visibility only (Invariant 2 — SPA gating is convenience,
// never the boundary).
//
// DISPLAY-ONLY lane: nothing here sends, reminds, or generates a notice document
// (Invariant 1). recordNotice RECORDS that a human sent a notice outside ITS.
//
// (R1) Errors: the helpers throw ApiError (src/lib/errorCopy.ts) — err.message is HUMAN
// copy, err.code the raw wire code (e.g. 'suspend_requires_nonpayment').

import { ApiError, raiseApiError } from "./errorCopy";
import type {
  PaymentCycleCreateResponse,
  PaymentsResponse,
  PaymentTermsDefaultResponse,
  PaymentTermsRow,
} from "../../worker/wire-types";

// Wire shapes — SINGLE-SOURCED in worker/wire-types.ts (the Worker types its c.json
// payloads with the same definitions, so a shape drift fails the typecheck on both
// sides); re-exported here so callers and test fixtures have one import path.
export type {
  PaymentCycleCreateResponse,
  PaymentCycleView,
  PaymentReceiptRow,
  PaymentsResponse,
  PaymentState,
  PaymentTermsDefaultResponse,
  PaymentTermsRow,
} from "../../worker/wire-types";

export { ApiError };

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { credentials: "same-origin" });
  if (!res.ok) return raiseApiError(res);
  return (await res.json()) as T;
}

async function sendJson<T>(method: "POST" | "PUT", url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method,
    credentials: "same-origin",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (!res.ok) return raiseApiError(res);
  return (await res.json()) as T;
}

/** The terms fields the editor submits (the Worker's readTermsFields bounds). */
export interface PaymentTermsDraft {
  billing_cadence: PaymentTermsRow["billing_cadence"];
  billing_cadence_detail?: string | null;
  net_days: number;
  nonpayment_notice_days: number;
  intent_to_suspend_days: number;
  notes?: string | null;
}

/** The whole Payments card in one read: terms (or null) + active cycles with receipts
 *  and their DERIVED states. */
export async function fetchPayments(jobId: string): Promise<PaymentsResponse> {
  return getJson<PaymentsResponse>(`/api/fieldops/payments?job_id=${encodeURIComponent(jobId)}`);
}

/** The same-client prefill (operator decision 6): the client's most recent OTHER job's
 *  terms, or {terms:null} — never an error on a client-less job. */
export async function fetchTermsDefault(jobId: string): Promise<PaymentTermsDefaultResponse> {
  return getJson<PaymentTermsDefaultResponse>(
    `/api/fieldops/payments/terms-default?job_id=${encodeURIComponent(jobId)}`,
  );
}

/** Upsert the job's ONE terms row. NOTE: editing terms never moves an existing cycle's
 *  stored due_date snapshot — only a submitted-date edit recomputes it (decision 10). */
export async function saveTerms(
  jobId: string,
  draft: PaymentTermsDraft,
): Promise<{ ok: boolean; terms: PaymentTermsRow }> {
  return sendJson("PUT", `/api/fieldops/payments/terms`, { job_id: jobId, ...draft });
}

/** Create a cycle (label required; a submitted date without terms lands with due_date
 *  NULL and the response note `no_terms_no_due_date`). */
export async function createCycle(
  jobId: string,
  draft: {
    label: string;
    invoice_submitted_date?: string | null;
    invoice_amount_cents?: number | null;
    note?: string | null;
  },
): Promise<PaymentCycleCreateResponse> {
  return sendJson("POST", `/api/fieldops/payments/cycles`, { job_id: jobId, ...draft });
}

/** Edit a cycle's content fields. A submitted-date CHANGE recomputes the due-date
 *  snapshot from the CURRENT terms; an unchanged one keeps it byte-for-byte. */
export async function editCycle(
  id: number,
  draft: {
    label: string;
    invoice_submitted_date?: string | null;
    invoice_amount_cents?: number | null;
    note?: string | null;
  },
): Promise<{ ok: boolean; id: number; due_date: string | null }> {
  return sendJson("POST", `/api/fieldops/payments/cycles/${id}/edit`, draft);
}

/** RECORD a notice date (the human sent the document outside ITS — nothing is generated
 *  or transmitted). Omitting the date stamps today Pacific; a re-post corrects the date.
 *  'suspend' refuses 409 suspend_requires_nonpayment until the nonpayment notice is
 *  recorded. */
export async function recordNotice(
  id: number,
  kind: "nonpayment" | "suspend",
  date?: string,
): Promise<{ ok: boolean; id: number; kind: string; date: string }> {
  return sendJson("POST", `/api/fieldops/payments/cycles/${id}/notice`, date ? { kind, date } : { kind });
}

/** Append a money-received event (partial payments are just more events). */
export async function addReceipt(
  id: number,
  draft: { amount_cents: number; received_date?: string; note?: string | null },
): Promise<{ ok: boolean; id: number; cycle_id: number }> {
  return sendJson("POST", `/api/fieldops/payments/cycles/${id}/receipt`, draft);
}

/** Soft-remove a cycle (idempotent — a repeat answers already_inactive). */
export async function deactivateCycle(
  id: number,
): Promise<{ ok: boolean; id: number; already_inactive?: boolean }> {
  return sendJson("POST", `/api/fieldops/payments/cycles/${id}/deactivate`);
}

/** Tombstone a wrong receipt (the correction path: deactivate + re-add). */
export async function deactivateReceipt(
  id: number,
): Promise<{ ok: boolean; id: number; already_inactive?: boolean }> {
  return sendJson("POST", `/api/fieldops/payments/receipts/${id}/deactivate`);
}
