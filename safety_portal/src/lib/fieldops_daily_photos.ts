// Daily-report additional-photo POOL client (DR-photo-pool Slice 1, 2026-07-03).
// Same-origin fetch with the session cookie. The Worker re-gates every call (session + the
// manager/admin role vocabulary + the per-job placement scope); these helpers drive UI only.
// Send-free (D1 reads/writes).
//
// WHY A POOL: the inline `site_photos` field is payload-budgeted (CS2: 280KB × 4 ≈ 1.49MB base64
// < the Worker's 1.8MB payload cap) — more inline photos structurally cannot ride the submission.
// Each ADDITIONAL photo uploads INDIVIDUALLY here; the submission carries only tiny REFERENCES
// (values.additional_photos = [{pool_id, caption?}]) that /api/submit validates + claims.
//
// OPTION D POSTURE (the G1 item-photo contract): record-only — the pool NEVER serves bytes back;
// the UI renders `status` chips only (pending "Screening…" / clean "Photo on file ✓" / refused
// retry copy) while the Slice-2 Mac §34 screen dispositions each row.
import { ApiError, raiseApiError } from "./errorCopy";
import type {
  DailyPhotosListResponse,
  DailyPhotoUploadResult,
  DailyPoolPhotoRow,
} from "../../worker/wire-types";
import type { PhotoValue } from "../forms/types";

export type {
  AdditionalPhotoRef,
  DailyPhotosListResponse,
  DailyPhotoUploadResult,
  DailyPoolPhotoRow,
} from "../../worker/wire-types";

/** UX mirror of the Worker's POOL_CAP_PER_DAY (worker/fieldops_daily_photos.ts) — the server is
 *  the boundary; this only sizes the "n/40" counter and disables the add button at the cap. */
export const POOL_CAP_PER_DAY = 40;

/** Upload ONE PhotoField-encoded photo into the (job, work_date) pool. The Worker's bounds 400
 *  carries the actionable machine reason in `detail` (photo_too_large / photo_bad_magic / …, the
 *  /api/submit convention) — preferred over the generic 'invalid_photo' so the manager gets
 *  field-actionable copy (the uploadItemPhoto pattern). */
export async function uploadDailyPhoto(
  jobId: string,
  workDate: string,
  photo: PhotoValue,
  // origin (0074): omitted for the field flow (server default 'field'); 'office_wpr' marks a
  // weekly-report-screen upload for the 90d retention window (capability-checked server-side).
  opts: { origin?: "office_wpr" } = {},
): Promise<DailyPhotoUploadResult> {
  const res = await fetch("/api/fieldops/daily-photo", {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      job_id: jobId, work_date: workDate, photo,
      ...(opts.origin ? { origin: opts.origin } : {}),
    }),
  });
  if (!res.ok) {
    let detail: string | null = null;
    try {
      const body = (await res.clone().json()) as { error?: unknown; detail?: unknown };
      if (body.error === "invalid_photo" && typeof body.detail === "string") detail = body.detail;
    } catch {
      /* non-JSON body → the shared handler below */
    }
    if (detail) {
      console.warn(`API error ${res.status}: invalid_photo/${detail} (${res.url})`);
      throw new ApiError(detail, res.status);
    }
    return raiseApiError(res);
  }
  return (await res.json()) as DailyPhotoUploadResult;
}

/** The actor's OWN pool rows for (job, work_date) — the screening-status read (chips pending →
 *  clean / refused) + draft-ref reconciliation. STATUS ONLY, never bytes. In AMEND mode pass the
 *  filed submission's uuid as `amendsUuid` (rides `amends=`): the Worker — after verifying it is
 *  the actor's own submission for this job/date — also returns the rows THAT submission claimed
 *  (claimed=1), so the amended report's filed photos chip "Photo on file ✓" instead of "missing". */
export async function listDailyPhotos(
  jobId: string,
  workDate: string,
  amendsUuid?: string | null,
): Promise<DailyPoolPhotoRow[]> {
  const amends = amendsUuid ? `&amends=${encodeURIComponent(amendsUuid)}` : "";
  const res = await fetch(
    `/api/fieldops/daily-photos?job_id=${encodeURIComponent(jobId)}&work_date=${encodeURIComponent(workDate)}${amends}`,
    { credentials: "same-origin" },
  );
  if (!res.ok) return raiseApiError(res);
  return ((await res.json()) as DailyPhotosListResponse).photos;
}

/** Pre-submit removal of an OWN pending/clean pool photo. Throws ApiError err.code
 *  'photo_claimed' when the row already belongs to a filed submission, 'not_deletable' for a
 *  refused forensic marker (drop the REF client-side instead), 'not_found' when pruned/foreign. */
export async function deleteDailyPhoto(poolId: number): Promise<void> {
  const res = await fetch(`/api/fieldops/daily-photo/${poolId}/delete`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json" },
    body: "{}",
  });
  if (!res.ok) return raiseApiError(res);
}
