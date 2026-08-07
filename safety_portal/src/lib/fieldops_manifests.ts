// Materials-manifest import API client (PR3b). Same-origin cookie fetch; no auth header — the
// bearer tiers are the Mac daemons' internal surface only, never the browser's.
//
// Every route is gated `cap.materials.manage` and the Worker re-gates each call; the SPA's
// capability check drives affordances only (Invariant 2 — SPA gating is convenience, never the
// boundary).
//
// (R1) Errors: getJson/postJson throw ApiError (src/lib/errorCopy.ts) — err.message is HUMAN copy,
// err.code is the raw wire code pages branch on (e.g. 'duplicate_manifest', 'not_committable').

import { ApiError, raiseApiError } from "./errorCopy";
import type {
  ManifestCommitResponse,
  ManifestDetailResponse,
  ManifestListResponse,
  ManifestPlanResponse,
  ManifestPreviewResponse,
  ManifestRowsResponse,
} from "../../worker/wire-types";

// Wire shapes — SINGLE-SOURCED in worker/wire-types.ts (the Worker types its c.json payloads with
// the same definitions, so a shape drift fails the typecheck on both sides); re-exported here so
// callers and test fixtures have one import path.
export type {
  ManifestColumnMap,
  ManifestCommitResponse,
  ManifestDetailResponse,
  ManifestGridRow,
  ManifestListResponse,
  ManifestListRow,
  ManifestPlanAbsent,
  ManifestPlanAmbiguous,
  ManifestPlanMatch,
  ManifestPlanResponse,
  ManifestPreviewResponse,
  ManifestRowKind,
  ManifestRowsResponse,
  ManifestStatus,
} from "../../worker/wire-types";

export { ApiError };

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { credentials: "same-origin" });
  if (!res.ok) return raiseApiError(res);
  return (await res.json()) as T;
}

async function postJson<T = { ok: boolean }>(url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (!res.ok) return raiseApiError(res);
  return (await res.json()) as T;
}

/** The MIME allowlist the Worker enforces — PDF + XLSX only, NARROWER than the estimate lane's.
 *  Every allowed type must be one the parser can actually read; accepting a .png would queue a
 *  document that can only ever end in `refused`, after it had already been screened and stored.
 *  Mirrored here so the file picker refuses locally instead of round-tripping a 422. */
export const MANIFEST_ACCEPT =
  ".pdf,.xlsx,application/pdf,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

/** Worker's MANIFEST_MAX_BYTES. Checked client-side so an oversize file fails instantly rather
 *  than after a 25 MB upload the server will refuse anyway. */
export const MANIFEST_MAX_BYTES = 25 * 1024 * 1024;

/** One page of the paged commit — matches the Worker's COMMIT_PAGE_LINES so `commitAll` below
 *  makes exactly as many round trips as the server would page into. */
export const COMMIT_PAGE_LINES = 100;

/** File → base64, without loading a second copy as a data: URL string. */
export async function fileToBase64(file: File): Promise<string> {
  const buf = new Uint8Array(await file.arrayBuffer());
  let bin = "";
  const STEP = 0x8000;
  for (let i = 0; i < buf.length; i += STEP) {
    bin += String.fromCharCode(...buf.subarray(i, i + STEP));
  }
  return btoa(bin);
}

export async function fetchManifests(jobId: string): Promise<ManifestListResponse> {
  return getJson<ManifestListResponse>(
    `/api/fieldops/manifests?job_id=${encodeURIComponent(jobId)}`,
  );
}

export async function fetchManifest(id: number): Promise<ManifestDetailResponse> {
  return getJson<ManifestDetailResponse>(`/api/fieldops/manifests/${id}`);
}

/** One page of the parsed grid. `after` is a `row_index` cursor — the grid is naturally ordered
 *  and stable, so a 900-row master BOM streams rather than arriving as one enormous response. */
export async function fetchManifestRows(
  id: number,
  opts: { after?: number; limit?: number } = {},
): Promise<ManifestRowsResponse> {
  const q = new URLSearchParams();
  if (opts.after !== undefined) q.set("after", String(opts.after));
  if (opts.limit !== undefined) q.set("limit", String(opts.limit));
  const qs = q.toString();
  return getJson<ManifestRowsResponse>(
    `/api/fieldops/manifests/${id}/rows${qs ? `?${qs}` : ""}`,
  );
}

/** Drain the whole grid by following the `row_index` cursor. Bounded by `maxRows` so a
 *  pathological manifest cannot spin the browser forever. */
export async function fetchAllManifestRows(
  id: number,
  { pageSize = 500, maxRows = 20_000 } = {},
): Promise<ManifestRowsResponse["rows"]> {
  const out: ManifestRowsResponse["rows"] = [];
  let after = 0;
  for (;;) {
    const { rows } = await fetchManifestRows(id, { after, limit: pageSize });
    out.push(...rows);
    if (rows.length < pageSize || out.length >= maxRows) return out;
    after = rows[rows.length - 1].row_index;
  }
}

export async function fetchManifestPreview(
  id: number,
  page: number,
): Promise<ManifestPreviewResponse> {
  return getJson<ManifestPreviewResponse>(`/api/fieldops/manifests/${id}/preview/${page}`);
}

export async function uploadManifest(
  jobId: string,
  file: File,
): Promise<{ ok: boolean; id: number | null; filename: string; size_bytes: number }> {
  const data_b64 = await fileToBase64(file);
  return postJson(`/api/fieldops/manifests`, {
    job_id: jobId,
    filename: file.name,
    mime: file.type,
    data_b64,
  });
}

export async function discardManifest(id: number): Promise<{ ok: boolean; id: number }> {
  return postJson(`/api/fieldops/manifests/${id}/discard`);
}

/** One resolved line, ready to become a `job_expected_materials` row. `source_row_index` is what
 *  makes the paged commit replay-safe — the watermark is expressed in SOURCE rows, not in "how
 *  many lines did we insert last time", so it stays meaningful across a re-ordered payload. */
export interface ManifestResolvedLine {
  source_row_index: number;
  description: string | null;
  part_number: string | null;
  category: string | null;
  qty: number | null;
  unit: string | null;
  expected_date: string | null;
  expected_ship_date: string | null;
  material_id: number | null;
}

export type ManifestMode = "merge" | "add_new";

/** DRY RUN. Writes nothing; returns what committing this set WOULD do against the job's live
 *  lines — including `ambiguous`, which the screen must resolve per row rather than let the
 *  server pick a winner. */
export async function planManifest(
  id: number,
  lines: ManifestResolvedLine[],
): Promise<ManifestPlanResponse> {
  return postJson<ManifestPlanResponse>(`/api/fieldops/manifests/${id}/plan`, { lines });
}

/** ONE page of the commit. Prefer `commitAll` unless you are driving the paging yourself. */
export async function commitManifest(
  id: number,
  mode: ManifestMode,
  lines: ManifestResolvedLine[],
): Promise<ManifestCommitResponse> {
  return postJson<ManifestCommitResponse>(`/api/fieldops/manifests/${id}/commit`, { mode, lines });
}

/**
 * Drive the paged commit to completion, re-posting the SAME full payload each round.
 *
 * That is deliberate and is what the watermark design buys: the server drops every line at or
 * below `committed_through_row` before writing, so re-sending the whole set is a no-op for the
 * pages that already landed. The client therefore needs no memory of what it sent — if a response
 * is lost mid-way, re-running this function resumes exactly where the server actually got to
 * rather than where the browser THINKS it got to.
 *
 * `onProgress` reports after each page so the screen can show honest progress on a 900-row BOM
 * instead of a spinner that looks hung.
 */
export async function commitAll(
  id: number,
  mode: ManifestMode,
  lines: ManifestResolvedLine[],
  onProgress?: (inserted: number, total: number) => void,
): Promise<{ inserted: number; pages: number }> {
  let inserted = 0;
  let pages = 0;
  // Bound the loop: each page must strictly advance the watermark, so at most one page per line
  // is possible. A server that stopped advancing would otherwise spin here forever.
  const maxPages = Math.ceil(lines.length / COMMIT_PAGE_LINES) + 2;
  for (;;) {
    const res = await commitManifest(id, mode, lines);
    inserted += res.inserted;
    pages += 1;
    onProgress?.(inserted, lines.length);
    if (res.done) return { inserted, pages };
    if (pages >= maxPages) {
      throw new Error(
        `commit did not finish after ${pages} pages (watermark ${res.committed_through_row}) — reload and check the manifest`,
      );
    }
  }
}
