import { useRef, useState } from "react";
import type * as checklist from "../lib/fieldops_checklist";
import { recordCountItem, uploadItemPhoto } from "../lib/fieldops_checklist";
import { ApiError } from "../lib/errorCopy";
import { itemTypeLabel } from "../lib/labels";
import { formCatalog, resolveFormTarget } from "../forms/registry";
import { encodePhoto } from "./PhotoField";

// Assigned-Tasks tab (P4 field-ops) — the shared per-item completion controls, used by BOTH the S3/S4
// daily checklist section AND the S6 assigned-inspection section (spec: the assigned section uses "the
// SAME completion controls as the daily section"). Renders the inner content of one checklist item <li>
// (the parent owns the <li> wrapper + list). Local note/count input state lives here; the parent owns
// busy-gating, the network call, and the post-mutation refresh via the callbacks. Per item type:
//   • manual_attest — a check (+ optional note) with an Undo + edit-note (idempotent re-complete) when
//     done; photo evidence renders when photo_ref is present (see the R3 photo note below).
//   • count         — a numeric input + Record; done → controls frozen, recorded value + Undo. When the
//     parent opts in via `onCountRecorded` (R3), the ROW owns the record call so a server 'below_target'
//     400 can drive the inline acknowledge-with-note flow (the R1 acknowledge path); without the opt-in
//     the legacy parent-owned `onRecordCount` path is byte-identical to the pre-R3 behavior.
//   • form_linked / inspection — a deep-link button (NOT manually checkable); auto-closes on a matching
//     submission (server loop-closure). A dead deep-link (unknown/retired form_code, or an assigned
//     instance without job+date) renders explanatory text instead of a silently disabled button; a done
//     item shows a static "Filed ✓" pill + a small "File another" link.
//
// PROP-COMPAT CONTRACT (R3, parallel-R2): the seven original props are FROZEN. `onComplete` gained an
// OPTIONAL third `photoRef` parameter (a narrower callback stays assignable, so existing callers
// typecheck and simply ignore it); `onCountRecorded` is NEW and OPTIONAL with a safe default (absent →
// the exact legacy count flow). Existing callers keep working unchanged.
//
// G1 PHOTO CAPTURE (supersedes the R3 "render half only" note — Option D RATIFIED 2026-07-03):
// the capture path now EXISTS — POST /api/fieldops/checklist/item-state/:id/photo queues ONE
// bounds-gated photo per item for the Mac §34 screen, and `photo_status` (pending|clean|refused,
// derived server-side from item_photos) drives the row's states:
//   none    → [Add photo] (reuses PhotoField's encodePhoto downscale/EXIF-sidecar ladder)
//   pending → "photo attached — screening…"
//   clean   → "photo on file ✓" (one-time 120ms scale-in — the same filed-pop motion, no third motion)
//   refused → refusal copy + retry (the refused slot is vacated server-side)
// NO IMAGE IS EVER RENDERED (Option D: no serving route exists, delete-on-screen; the photo's
// permanent record is Box). The former raw `data:image/` passthrough branch is RETIRED — an
// attacker-writable photo_ref must never place bytes in a viewer's DOM; any legacy ref renders
// the neutral "photo attached" marker. The affordance renders ONLY when the parent opts in via
// `onPhotoUploaded` (the assigned-inspections surface); other callers (admin preview) are
// pixel-identical to pre-G1.

// Status → pill class for a checklist item state (done = ok, else neutral).
function itemPill(status: string): string {
  return status === "done" ? "dash-pill dash-pill--ok" : "dash-pill";
}

/** Copy for a form_linked/inspection item whose deep-link cannot open (R3 dead-end explanations). */
export const DEADEND_FORM_UNAVAILABLE = "This item points at a form that isn't available — tell the office.";
export const DEADEND_NO_JOB_DATE = "This item needs a job and date before its form can be opened — tell the office.";

/** G1 item-photo lifecycle copy (status-only rendering — no image is ever shown; Option D). */
export const PHOTO_PENDING_COPY = "photo attached — screening…";
export const PHOTO_ON_FILE_COPY = "photo on file ✓";
export const PHOTO_REFUSED_COPY =
  "This photo was refused by the security screen and wasn't saved — you can attach a different one.";
export const PHOTO_UNUSABLE_COPY = "That file could not be processed as a photo.";

export function ChecklistItemRow({
  item,
  busy,
  canOpenForm,
  onComplete,
  onUncomplete,
  onRecordCount,
  onOpenForm,
  onCountRecorded,
  onPhotoUploaded,
}: {
  item: checklist.ChecklistItemState;
  busy: boolean;
  canOpenForm: boolean;
  /** photoRef (R3, optional 3rd arg) threads an existing photo_ref through an idempotent re-complete
   * (edit-note) so it isn't clobbered; legacy 2-param callers remain assignable and just drop it. */
  onComplete: (item: checklist.ChecklistItemState, note?: string, photoRef?: string) => void;
  onUncomplete: (item: checklist.ChecklistItemState) => void;
  onRecordCount: (item: checklist.ChecklistItemState, value: number) => void;
  onOpenForm: (item: checklist.ChecklistItemState) => void;
  /** R3 opt-in: when present, the ROW owns the count record call (recordCountItem) so a server
   * 'below_target' drives the inline acknowledge-with-note flow; called after each successful
   * record/acknowledge so the parent can refetch. Absent → the legacy onRecordCount path, unchanged. */
  onCountRecorded?: () => void | Promise<void>;
  /** G1 opt-in: when present, the ROW owns the photo capture flow (encodePhoto → uploadItemPhoto)
   * and renders the photo affordance/states; called after a successful upload so the parent can
   * refetch (photo_status flips to 'pending' server-side). Absent → no photo UI, byte-identical
   * to the pre-G1 row (prop-compat: OPTIONAL, the frozen prop set is untouched). */
  onPhotoUploaded?: () => void | Promise<void>;
}) {
  const [note, setNote] = useState("");
  const [count, setCount] = useState(item.value_num !== null ? String(item.value_num) : "");
  // R3 — done-manual edit-note affordance (idempotent re-complete).
  const [editingNote, setEditingNote] = useState(false);
  const [editNote, setEditNote] = useState("");
  // R3 — row-owned count flow (only used when onCountRecorded is provided).
  const [countBusy, setCountBusy] = useState(false);
  const [countErr, setCountErr] = useState<string | null>(null);
  const [belowTarget, setBelowTarget] = useState<{ value: number } | null>(null);
  const [ackNote, setAckNote] = useState("");
  // G1 — row-owned photo capture flow (only used when onPhotoUploaded is provided).
  const photoInputRef = useRef<HTMLInputElement>(null);
  const [photoBusy, setPhotoBusy] = useState(false);
  const [photoErr, setPhotoErr] = useState<string | null>(null);

  const done = item.status === "done";
  const isManual = item.item_type === "manual_attest";
  const isCount = item.item_type === "count";
  const isLinked = item.item_type === "form_linked" || item.item_type === "inspection";

  // requires_photo gate — an item authored "requires photo" can't be marked done until a live
  // (pending|clean) photo is on file. The Worker re-enforces at /complete (defense-in-depth); this
  // disables the button + says why, so a tap is never silently rejected. A refused/absent photo
  // blocks; an already-done item is never re-gated (only completing demands the photo).
  const photoAttached = item.photo_status === "pending" || item.photo_status === "clean";
  const needsPhoto = !!item.requires_photo && !photoAttached;

  // Row-owned count record (R3): server-driven below-target detection → inline acknowledge prompt.
  async function recordOwned(value: number) {
    if (!Number.isFinite(value) || value < 0) {
      setCountErr("Enter a number.");
      return;
    }
    setCountBusy(true);
    setCountErr(null);
    try {
      await recordCountItem(item.id, value);
      setBelowTarget(null);
      setAckNote("");
      await onCountRecorded?.();
    } catch (err) {
      if (err instanceof ApiError && err.code === "below_target") {
        setBelowTarget({ value }); // the value IS recorded server-side; offer the acknowledge flow
      } else {
        setCountErr(err instanceof Error ? err.message : "Update failed.");
      }
    } finally {
      setCountBusy(false);
    }
  }

  // The R1 acknowledge path: complete BELOW target with a required explanatory note.
  async function acknowledgeShortfall() {
    if (!belowTarget) return;
    const trimmed = ackNote.trim();
    if (trimmed.length === 0) return; // button is disabled without a note; belt-and-suspenders
    setCountBusy(true);
    setCountErr(null);
    try {
      await recordCountItem(item.id, belowTarget.value, { acknowledgeBelowTarget: true, note: trimmed });
      setBelowTarget(null);
      setAckNote("");
      await onCountRecorded?.();
    } catch (err) {
      setCountErr(err instanceof Error ? err.message : "Update failed.");
    } finally {
      setCountBusy(false);
    }
  }

  // G1 — photo capture flow (row-owned; see the module header). encodePhoto is PhotoField's
  // downscale/EXIF-sidecar ladder (reused, not cloned); the Worker re-enforces every bound.
  async function onPhotoFile(list: FileList | null) {
    if (!list || list.length === 0) return;
    setPhotoBusy(true);
    setPhotoErr(null);
    try {
      const encoded = await encodePhoto(list[0], 1);
      if (!encoded) {
        setPhotoErr(PHOTO_UNUSABLE_COPY);
        return;
      }
      await uploadItemPhoto(item.id, encoded);
      await onPhotoUploaded?.();
    } catch (err) {
      // errorCopy human copy rides err.message (bounds details, the one-photo 409, ownership).
      setPhotoErr(err instanceof Error ? err.message : "The photo could not be uploaded.");
    } finally {
      setPhotoBusy(false);
      if (photoInputRef.current) photoInputRef.current.value = "";
    }
  }

  // G1 — the photo affordance + lifecycle states (STATUS-ONLY — no image is ever rendered;
  // Option D). Only when the parent opts in AND the item is a checkable (evidence-bearing) type;
  // form_linked/inspection evidence is the filed submission itself (the Worker refuses them too).
  const photoCapture =
    onPhotoUploaded && (isManual || isCount) ? (
      item.photo_status === "pending" ? (
        <span className="dash-card__sub"> · {PHOTO_PENDING_COPY}</span>
      ) : item.photo_status === "clean" ? (
        <span className="dash-card__sub checklist-photo-filed"> · {PHOTO_ON_FILE_COPY}</span>
      ) : (
        <>
          {item.photo_status === "refused" ? (
            <span className="dash-card__sub" role="alert">
              {" "}· {PHOTO_REFUSED_COPY}
            </span>
          ) : null}{" "}
          <input
            ref={photoInputRef}
            type="file"
            accept="image/*"
            hidden
            data-testid={`item-photo-input-${item.id}`}
            onChange={(e) => {
              void onPhotoFile(e.target.files);
            }}
          />
          <button
            type="button"
            // A required-but-missing photo makes "Add photo" the obvious next action (primary), so the
            // person isn't left staring at a disabled "Mark done"; an optional photo stays quiet.
            // --secondary, NOT --ghost: this row sits on a white card, where the header ghost's white
            // text + 60%-white border is invisible (global.css: "the trackers' page-context ghosts are
            // remapped to --secondary in the markup so they are visible on the white page").
            className={needsPhoto ? "btn btn--primary" : "btn btn--secondary"}
            aria-label={`Add photo for item ${item.id}`}
            disabled={busy || photoBusy}
            onClick={() => photoInputRef.current?.click()}
          >
            {photoBusy
              ? "Uploading…"
              : item.photo_status === "refused"
                ? "Add a different photo"
                : needsPhoto
                  ? "📷 Add photo"
                  : "Add photo"}
          </button>
          {photoErr ? (
            <span className="login__error" role="alert">
              {" "}{photoErr}
            </span>
          ) : null}
        </>
      )
    ) : null;

  // Legacy photo_ref marker (pre-G1 refs, e.g. a box:<id> stamped by a completion body). The G1
  // lifecycle states above own anything with an item_photos row (photo_status non-null); this
  // renders only for refs WITHOUT one, and NEVER as an image — the raw `data:image/` passthrough
  // is retired (an attacker-writable ref must not place bytes in a viewer's DOM; Option D).
  const photoEvidence =
    item.photo_ref && item.photo_status == null ? (
      <span className="dash-card__sub" title={item.photo_ref}> · photo attached</span>
    ) : null;

  // R3 — an acknowledged shortfall stays flagged after completion (value below target, but done).
  const doneBelowTarget =
    done && item.value_num !== null && item.target_count !== null && item.value_num < item.target_count;

  // R3 — deep-link openability (dead-end explanations instead of silently disabled buttons). The
  // catalog check mirrors resolveFormTarget's lookup: an unknown/retired form_code resolves to a
  // parentCode that isn't in the catalog.
  let linkedControls = null;
  if (isLinked) {
    const target = item.form_code ? resolveFormTarget(item.form_code) : null;
    const formAvailable = target !== null && formCatalog().some((p) => p.parent_form_code === target.parentCode);
    const canOpen = canOpenForm && formAvailable;
    if (done) {
      linkedControls = (
        <>
          {" "}
          <span className="dash-pill dash-pill--ok">Filed ✓</span>
          {item.filed_by ? <span className="dash-card__sub"> · filed by {item.filed_by}</span> : null}
          {canOpen ? (
            <>
              {" "}
              <button
                type="button"
                className="btn btn--secondary"
                aria-label={`File another ${item.label ?? `form for item ${item.id}`}`}
                disabled={busy}
                onClick={() => onOpenForm(item)}
              >
                File another
              </button>
            </>
          ) : null}
        </>
      );
    } else if (!formAvailable) {
      linkedControls = (
        <span className="dash-card__sub" role="note">
          {" "}{DEADEND_FORM_UNAVAILABLE}
        </span>
      );
    } else if (!canOpenForm) {
      linkedControls = (
        <span className="dash-card__sub" role="note">
          {" "}{DEADEND_NO_JOB_DATE}
        </span>
      );
    } else {
      linkedControls = (
        <>
          {" "}
          <button
            type="button"
            className="btn btn--primary"
            aria-label={`Complete ${item.label ?? `item ${item.id}`}`}
            disabled={busy}
            onClick={() => onOpenForm(item)}
          >
            {`Complete ${item.label ?? "form"}`}
          </button>
        </>
      );
    }
  }

  return (
    <>
      <span className={itemPill(item.status)}>{done ? "done" : "pending"}</span> {item.label}
      <span className="dash-card__sub"> · {itemTypeLabel(item.item_type)}</span>
      {isManual ? (
        <>
          {!done && (
            <>
              {" "}
              <input
                type="text"
                aria-label={`Note for item ${item.id}`}
                placeholder="note (optional)"
                value={note}
                onChange={(e) => setNote(e.target.value)}
                disabled={busy}
              />
            </>
          )}{" "}
          <button
            type="button"
            className={done ? "btn btn--secondary" : "btn btn--primary"}
            aria-label={done ? `Undo item ${item.id}` : `Complete item ${item.id}`}
            disabled={busy || (!done && needsPhoto)}
            onClick={() =>
              // G1: thread the EXISTING photo_ref through a plain "Mark done" (the complete route
              // overwrites photo_ref from the body — omitting it would NULL the 'pending:<id>'
              // stamp; same threading the edit-note path below always did).
              done ? onUncomplete(item) : onComplete(item, note || undefined, item.photo_ref ?? undefined)
            }
          >
            {done ? "Undo" : "Mark done"}
          </button>
          {!done && needsPhoto ? (
            <span className="checklist-req-photo-hint" role="note"> · 📷 Photo required — attach one first</span>
          ) : null}
          {done && item.note && !editingNote ? <span className="dash-card__sub"> · {item.note}</span> : null}
          {done ? photoEvidence : null}
          {done && !editingNote ? (
            <>
              {" "}
              <button
                type="button"
                className="btn btn--secondary"
                aria-label={`Edit note for item ${item.id}`}
                disabled={busy}
                onClick={() => {
                  setEditNote(item.note ?? "");
                  setEditingNote(true);
                }}
              >
                {item.note ? "Edit note" : "Add note"}
              </button>
            </>
          ) : null}
          {done && editingNote ? (
            <>
              {" "}
              <input
                type="text"
                aria-label={`New note for item ${item.id}`}
                placeholder="note"
                value={editNote}
                onChange={(e) => setEditNote(e.target.value)}
                disabled={busy}
              />{" "}
              <button
                type="button"
                className="btn btn--secondary"
                aria-label={`Save note for item ${item.id}`}
                disabled={busy}
                onClick={() => {
                  // Idempotent re-complete: the server overwrites note (and photo_ref — thread the
                  // existing one through so a note edit doesn't clobber photo evidence).
                  onComplete(item, editNote.trim() || undefined, item.photo_ref ?? undefined);
                  setEditingNote(false);
                }}
              >
                Save note
              </button>
            </>
          ) : null}
        </>
      ) : isCount ? (
        <>
          {item.target_count !== null ? (
            <span className="dash-card__sub"> · target {item.target_count}</span>
          ) : null}
          {done ? (
            // R3 — frozen when done: recorded value + Undo, no live controls.
            <>
              <span className="dash-card__sub"> · recorded {item.value_num ?? "—"}</span>
              {doneBelowTarget ? (
                <>
                  {" "}
                  <span className="dash-pill dash-pill--warn">below target</span>
                </>
              ) : null}
              {item.note ? <span className="dash-card__sub"> · {item.note}</span> : null}{" "}
              <button
                type="button"
                className="btn btn--secondary"
                aria-label={`Undo item ${item.id}`}
                disabled={busy || countBusy}
                onClick={() => onUncomplete(item)}
              >
                Undo
              </button>
            </>
          ) : (
            <>
              {" "}
              <input
                type="number"
                inputMode="numeric"
                min={0}
                aria-label={`Count for item ${item.id}`}
                placeholder="count"
                value={count}
                onChange={(e) => setCount(e.target.value)}
                disabled={busy || countBusy}
              />{" "}
              <button
                type="button"
                className="btn btn--primary"
                aria-label={`Record item ${item.id}`}
                disabled={busy || countBusy}
                onClick={() => {
                  const value = count === "" ? NaN : Number(count);
                  if (onCountRecorded) void recordOwned(value);
                  else onRecordCount(item, value); // legacy parent-owned path (pre-R3, unchanged)
                }}
              >
                Record
              </button>
              {item.value_num !== null ? (
                // R3 — the recorded value stays visible while the item is open (a below-target
                // record persists server-side even though the item didn't complete).
                <span className="dash-card__sub"> · recorded {item.value_num}</span>
              ) : null}
              {belowTarget ? (
                // R3 — inline acknowledge-with-note prompt after a server 'below_target' 400 (the
                // R1 acknowledge path; the note is REQUIRED server-side).
                <span role="alert">
                  {" "}
                  <span className="dash-pill dash-pill--warn">below target</span>{" "}
                  <span className="dash-card__sub">
                    Recorded {belowTarget.value}
                    {item.target_count !== null ? ` of target ${item.target_count}` : ""} — the item stays
                    open unless you record it anyway with a note.
                  </span>{" "}
                  <input
                    type="text"
                    aria-label={`Shortfall note for item ${item.id}`}
                    placeholder="why is it below target? (required)"
                    value={ackNote}
                    onChange={(e) => setAckNote(e.target.value)}
                    disabled={busy || countBusy}
                  />{" "}
                  <button
                    type="button"
                    className="btn btn--secondary"
                    aria-label={`Record item ${item.id} anyway`}
                    disabled={busy || countBusy || ackNote.trim().length === 0}
                    onClick={() => void acknowledgeShortfall()}
                  >
                    Record anyway
                  </button>{" "}
                  <button
                    type="button"
                    className="btn btn--secondary"
                    aria-label={`Dismiss shortfall prompt for item ${item.id}`}
                    disabled={busy || countBusy}
                    onClick={() => {
                      setBelowTarget(null);
                      setAckNote("");
                    }}
                  >
                    Cancel
                  </button>
                </span>
              ) : null}
              {countErr ? (
                <span className="login__error" role="alert">
                  {" "}{countErr}
                </span>
              ) : null}
            </>
          )}
        </>
      ) : isLinked ? (
        linkedControls
      ) : null}
      {photoCapture}
    </>
  );
}

// Type-level guard for the R3 prop-compat contract: a pre-R3 caller's callbacks (2-param onComplete,
// no onCountRecorded) must remain assignable forever. This compiles — or the props broke.
type _LegacyOnComplete = (item: checklist.ChecklistItemState, note?: string) => void;
type _AssertCompat = _LegacyOnComplete extends Parameters<typeof ChecklistItemRow>[0]["onComplete"]
  ? true
  : never;
const _compatCheck: _AssertCompat = true;
void _compatCheck;
