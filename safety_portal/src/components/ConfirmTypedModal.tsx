import { useEffect, useRef, useState } from "react";

/**
 * A confirmation dialog that requires the operator to TYPE an exact phrase before the action arms.
 *
 * The SPA's first real modal — every other confirmation in this app is a `window.confirm`. Those
 * are fine for "delete this row?"; they are not fine for an action that relocates a job's folders
 * across two external systems, because a native confirm gives no room to say WHAT will move and
 * accepts a reflexive Enter.
 *
 * This is a usability affordance, NOT the security control. The Worker re-checks `confirm` against
 * the row's own project_name server-side (`archiveTransition` in worker/fieldops_job_write.ts) —
 * a caller who skips this dialog entirely still gets refused. Keeping the real check server-side is
 * what makes "wrong job open in a second tab" a refusal instead of an archive.
 */
export function ConfirmTypedModal({
  open,
  title,
  expected,
  body,
  confirmLabel,
  confirmClass = "btn btn--retire",
  busy = false,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  /** The exact phrase the operator must type. Compared trim-only and case-SENSITIVELY. */
  expected: string;
  body: React.ReactNode;
  confirmLabel: string;
  confirmClass?: string;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const [typed, setTyped] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  // Clear on every open so a previous attempt's text can never pre-arm the button.
  useEffect(() => {
    if (open) {
      setTyped("");
      // Focus the input, not the confirm button — the operator should land where the work is.
      const t = setTimeout(() => inputRef.current?.focus(), 0);
      return () => clearTimeout(t);
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onCancel]);

  if (!open) return null;

  // Trim-only, case-sensitive — the SAME rule the Worker applies, so the button's enabled state and
  // the server's verdict can never disagree and leave the operator staring at a live button that
  // 409s.
  const matches = typed.trim() === expected.trim();
  const armed = matches && !busy;

  return (
    <div className="modal-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) onCancel(); }}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={title}>
        <h3 className="modal__title">{title}</h3>
        <div className="modal__body">{body}</div>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            // Guarded identically to the button: Enter on a partial match must be a no-op, not a
            // shortcut past the confirmation.
            if (armed) onConfirm();
          }}
        >
          <label className="dash-card__label modal__field">
            Type <code>{expected}</code> to confirm:{" "}
            <input
              ref={inputRef}
              type="text"
              value={typed}
              disabled={busy}
              onChange={(e) => setTyped(e.target.value)}
              aria-label="Confirmation phrase"
              autoComplete="off"
            />
          </label>
          <div className="dash-row modal__actions">
            <button type="button" className="btn btn--secondary" onClick={onCancel} disabled={busy}>
              Cancel
            </button>{" "}
            <button type="submit" className={confirmClass} disabled={!armed}>
              {busy ? "Working…" : confirmLabel}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
