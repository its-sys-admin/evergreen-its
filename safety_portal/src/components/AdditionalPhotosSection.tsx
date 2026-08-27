import { useEffect, useRef, useState } from "react";
import { encodePhoto } from "./PhotoField";
import {
  POOL_CAP_PER_DAY,
  deleteDailyPhoto,
  listDailyPhotos,
  uploadDailyPhoto,
  type AdditionalPhotoRef,
  type DailyPoolPhotoRow,
} from "../lib/fieldops_daily_photos";
import { ApiError } from "../lib/errorCopy";
import type { ItemPhotoStatus } from "../lib/fieldops_checklist";

// ─────────────────────────────────────────────────────────────────────────────
// Additional-photos pool section (DR-photo-pool Slice 1, operator directive 2026-07-03:
// "add more photo holding sections … as many of those as you need in the daily field report").
//
// Rendered by FormRenderer's `additional_photos` case when the HOST (the Daily tab) supplies the
// AdditionalPhotosAdapter (job + date scope). DELIBERATELY SELF-CONTAINED — a separate component
// from PhotoField/DailyReportTab (a parallel bugfix slice is touching their draft-photo
// interaction; this file touches neither — it only REUSES PhotoField's exported encodePhoto,
// the canonical client-side downscale/EXIF-strip encoder).
//
// TRANSPORT: the inline 4-photo site_photos field is payload-budgeted (CS2: 280KB × 4 ≈ 1.49MB
// base64 < the Worker's 1.8MB payload cap) — more inline photos CANNOT ride the submission. Each
// photo added here uploads IMMEDIATELY + INDIVIDUALLY to the pool (POST /api/fieldops/daily-photo,
// its own bounded request); the form values (and the sessionStorage draft) carry only tiny
// REFERENCES ([{pool_id, caption?}]) — no draft-quota pressure. /api/submit validates + claims
// the references.
//
// OPTION D (the G1 posture): the pool never serves bytes back — after upload the UI shows the
// G1 screening-state chip vocabulary (pending "Screening…" / clean "Photo on file ✓" / refused
// retry copy), never a thumbnail. A draft restore therefore shows chips, not images.
// ─────────────────────────────────────────────────────────────────────────────

/** A ref's displayed lifecycle: the pool row's screening status, "on_file" for a row claimed by
 *  the amends target (the amended report's OWN filed photo — the amend read is the only way a
 *  claimed row reaches the SPA), "missing" when the row vanished (pruned / claimed elsewhere —
 *  the draft-restore edge), or null while unknown. */
type RefStatus = ItemPhotoStatus | "on_file" | "missing" | null;

const CHIP_COPY: Record<Exclude<RefStatus, null>, string> = {
  pending: "Screening…",
  clean: "Photo on file ✓",
  on_file: "Photo on file ✓",
  refused: "Refused — remove it and retry with a different photo",
  missing: "No longer available — remove it before submitting",
};

/** Map a pool row (or its absence) to the chip vocabulary. A claimed row is the amended report's
 *  own filed photo — "on_file" (removal drops the REF only, never a pool delete: the pool row is
 *  the FILED report's record linkage) unless screening refused it, in which case the refused
 *  retry copy still wins. */
function chipStatus(row: DailyPoolPhotoRow | undefined): Exclude<RefStatus, null> {
  if (!row) return "missing";
  if (row.claimed && row.status !== "refused") return "on_file";
  return row.status;
}

const MAX_CAPTION = 300; // mirror of the Worker's per-ref caption bound (UX only)

interface Props {
  title?: string;
  jobId: string;
  workDate: string;
  /** The filed submission being AMENDED, else null — rides the list read's `amends=` param so
   *  the amended report's own claimed rows resolve "on_file" instead of "missing". */
  amendsUuid?: string | null;
  /** The submission's pool references (values.additional_photos — draft-persisted by the host). */
  refs: AdditionalPhotoRef[];
  onChange: (next: AdditionalPhotoRef[]) => void;
}

export function AdditionalPhotosSection({ title, jobId, workDate, amendsUuid, refs, onChange }: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [statusWarn, setStatusWarn] = useState("");
  const [statusById, setStatusById] = useState<Record<number, RefStatus>>({});

  const room = Math.max(0, POOL_CAP_PER_DAY - refs.length);

  // Screening-status read: reconcile the refs against the pool (chips pending → clean/refused;
  // a ref whose row vanished → "missing"). Fires only when there ARE refs to reconcile — a
  // pristine section performs no network. Failures are a soft warn, never a blocker (the
  // statuses degrade to "unknown"; submit still works — the Worker is the boundary).
  async function refreshStatuses(currentRefs: AdditionalPhotoRef[]): Promise<void> {
    if (currentRefs.length === 0) return;
    try {
      const rows = await listDailyPhotos(jobId, workDate, amendsUuid);
      const byId = new Map(rows.map((r) => [r.id, r]));
      setStatusById((prev) => {
        const next: Record<number, RefStatus> = { ...prev };
        for (const r of currentRefs) next[r.pool_id] = chipStatus(byId.get(r.pool_id));
        return next;
      });
      setStatusWarn("");
    } catch {
      setStatusWarn("Couldn't check photo screening status — the photos are unaffected. Retry in a moment.");
    }
  }

  // One reconciliation per (job, date, amends) scope — the draft-restore moment, and the
  // load-&-amend moment (amendsUuid flips null → the filed uuid AFTER mount, re-firing this with
  // the amend read that resolves the filed report's claimed rows "on_file"). Uploads maintain
  // their own optimistic 'pending' entry, so this doesn't need to re-fire per ref change.
  useEffect(() => {
    let active = true;
    if (refs.length === 0) return;
    void (async () => {
      try {
        const rows = await listDailyPhotos(jobId, workDate, amendsUuid);
        if (!active) return;
        const byId = new Map(rows.map((r) => [r.id, r]));
        setStatusById((prev) => {
          const next: Record<number, RefStatus> = { ...prev };
          for (const r of refs) next[r.pool_id] = chipStatus(byId.get(r.pool_id));
          return next;
        });
        setStatusWarn("");
      } catch {
        if (active) {
          setStatusWarn("Couldn't check photo screening status — the photos are unaffected. Retry in a moment.");
        }
      }
    })();
    return () => {
      active = false;
    };
    // Refs are captured at scope-change time on purpose (see comment above).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, workDate, amendsUuid]);

  const onFiles = async (list: FileList | null) => {
    if (!list || list.length === 0) return;
    setBusy(true);
    setError("");
    const added: AdditionalPhotoRef[] = [];
    const notes: string[] = [];
    let failed = 0;
    for (const file of Array.from(list).slice(0, room)) {
      // The canonical client encoder (downscale ladder + EXIF caption-then-strip re-encode).
      const p = await encodePhoto(file, refs.length + added.length + 1);
      if (!p) {
        failed += 1;
        continue;
      }
      try {
        // Straight to the pool — its own bounded request; the form only keeps the reference.
        const res = await uploadDailyPhoto(jobId, workDate, p);
        added.push({ pool_id: res.pool_id, caption: p.name });
        setStatusById((prev) => ({ ...prev, [res.pool_id]: "pending" }));
      } catch (e) {
        if (e instanceof ApiError && (e.code === "pool_cap_reached" || e.code === "pool_backlogged")) {
          notes.push(e.message); // the mapped human copy; no point trying the remaining files
          break;
        }
        notes.push(e instanceof Error ? e.message : "Upload failed — try again.");
        failed += 1;
      }
    }
    if (list.length > room) {
      notes.push(`Limit is ${POOL_CAP_PER_DAY} additional photos per day — extra files were skipped.`);
    }
    if (failed > 0) notes.push(`${failed} file${failed === 1 ? "" : "s"} could not be added.`);
    setError([...new Set(notes)].join(" "));
    if (added.length > 0) onChange([...refs, ...added]);
    setBusy(false);
    if (inputRef.current) inputRef.current.value = "";
  };

  const removeRef = async (ref: AdditionalPhotoRef) => {
    const status = statusById[ref.pool_id] ?? null;
    setError("");
    // A refused row is a byte-free forensic marker (not deletable), a missing row is already
    // gone, and an on_file row belongs to the FILED report being amended (its claim is that
    // report's record linkage — the Worker would 409 photo_claimed) — all three just drop the
    // REFERENCE (an on-file drop means "this amendment no longer carries that photo"; the filed
    // report keeps its claim). Live pending/clean rows are deleted from the pool first so they
    // don't count against the day's cap.
    if (status !== "refused" && status !== "missing" && status !== "on_file") {
      try {
        await deleteDailyPhoto(ref.pool_id);
      } catch (e) {
        if (e instanceof ApiError && (e.code === "not_found" || e.code === "not_deletable")) {
          // pruned/foreign or a refused forensic marker — the ref is stale either way; drop it
        } else {
          setError(e instanceof Error ? e.message : "Couldn't remove the photo — try again.");
          return;
        }
      }
    }
    onChange(refs.filter((r) => r.pool_id !== ref.pool_id));
  };

  const setCaption = (poolId: number, caption: string) => {
    onChange(refs.map((r) => (r.pool_id === poolId ? { ...r, caption: caption.slice(0, MAX_CAPTION) } : r)));
  };

  return (
    <section className="fr__section fr__additional-photos">
      <h2 className="fr__section-title">{title ?? "Additional site photos"}</h2>
      {/* Form-agnostic copy (Photos program, 2026-08-27): the pool mounts on any form now,
          so the helper no longer references the daily report's "four site photos above". */}
      <p className="fr__form-link-helper muted">
        Add as many photos as you need here — each uploads right away and is screened before
        filing ({refs.length}/{POOL_CAP_PER_DAY}).
      </p>
      {statusWarn ? (
        <p className="dash-unavail" role="status">
          {statusWarn}{" "}
          <button type="button" className="btn btn--secondary" onClick={() => void refreshStatuses(refs)}>
            Retry
          </button>
        </p>
      ) : null}
      {refs.length > 0 ? (
        <ul className="dash-tasklist additional-photos__list">
          {refs.map((r, i) => {
            const status = statusById[r.pool_id] ?? null;
            return (
              <li key={r.pool_id}>
                <span className="dash-chip" data-status={status ?? "unknown"}>
                  {status ? CHIP_COPY[status] : "Status unknown"}
                </span>{" "}
                <label className="field additional-photos__caption">
                  <span className="field__label">Caption (photo {i + 1})</span>
                  <input
                    className="field__input"
                    type="text"
                    maxLength={MAX_CAPTION}
                    value={r.caption ?? ""}
                    onChange={(e) => setCaption(r.pool_id, e.target.value)}
                  />
                </label>
                <button
                  type="button"
                  className="btn btn--secondary"
                  aria-label={`Remove additional photo ${i + 1}`}
                  onClick={() => void removeRef(r)}
                >
                  Remove
                </button>
              </li>
            );
          })}
        </ul>
      ) : null}
      <input
        ref={inputRef}
        type="file"
        accept="image/*"
        multiple
        hidden
        data-testid="additional-photos-input"
        onChange={(e) => {
          void onFiles(e.target.files);
        }}
      />
      <div className="dash-row">
        <button
          type="button"
          className="btn btn--secondary"
          disabled={busy || room === 0}
          onClick={() => inputRef.current?.click()}
        >
          {busy ? "Uploading…" : room === 0 ? "Photo limit reached" : "+ Add more photos"}
        </button>{" "}
        {refs.length > 0 ? (
          <button
            type="button"
            className="btn btn--secondary"
            onClick={() => void refreshStatuses(refs)}
          >
            Check screening status
          </button>
        ) : null}
      </div>
      {error ? (
        <p className="photo-field__error" role="alert">
          {error}
        </p>
      ) : null}
    </section>
  );
}
