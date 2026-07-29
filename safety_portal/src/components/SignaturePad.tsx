import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { PointerEvent as ReactPointerEvent } from "react";

interface SignaturePadProps {
  width?: number;
  height?: number;
  strokeWidth?: number;
  /**
   * CONTROLLED when provided — including "" — : the committed signature `d`. The
   * preview AND the capture sheet's draft seed both read THIS, so the pad can never
   * disagree with the value that will actually be filed. Omit (`undefined`) for
   * uncontrolled mode (internal state).
   *
   * Why it exists: on an AMEND the prior signature is repopulated into form values,
   * but the host does NOT remount the pad — so without `value` the pad rendered blank
   * over a real, submittable signature. Since the inline element became a read-only
   * preview, blank reads as "not signed", so a user could amend, never tap it, submit,
   * and file the amended legal PDF carrying the OLD ink. It also closes the
   * index-keyed `signature_table` desync: removing a row shifts values up while React
   * reuses the component instance, so an uncontrolled pad paints the DELETED person's
   * signature onto the next row.
   *
   * If a parent passes `value` but never echoes it back, the pad is inert by design:
   * Done fires `onChange` and the preview reverts to the prop. All live hosts round-trip.
   */
  value?: string;
  /**
   * Fires ONCE per sheet session, the first time a real stroke completes (movement,
   * not a stray tap). Hosts use it to arm an unsaved-work guard.
   *
   * With commit-on-Done, a user whose FIRST action is signing is invisible to a dirty
   * flag until Done — so hardware Back or a tab close mid-signing discarded the form
   * with no prompt, where every completed stroke used to arm one. Deliberately NOT
   * fired on sheet OPEN: open-then-Cancel must stay clean.
   */
  onDraftDirty?: () => void;
  /**
   * Fires when the capture sheet is COMMITTED via Done, and on Clear.
   * svgPath = combined SVG path `d`; empty = no signature.
   *
   * NOTE (deliberate behaviour change): this used to fire at the end of EVERY
   * stroke. Capture now happens in a full-screen sheet that holds draft strokes
   * locally, so abandoned attempts (Cancel) never reach form state.
   */
  onChange?: (svgPath: string, isEmpty: boolean) => void;
}

// ─────────────────────────────────────────────────────────────────────────────
// COORDINATE-SPACE CONTRACT — do not change without changing form_pdf.py.
//
// The emitted `d` lives in a 600x180 space. That number is duplicated, out of
// band, in `safety_reports/form_pdf.py` (`_SIG_W, _SIG_H = 600.0, 180.0`), and
// NOTHING travels with the path to describe its geometry — not in payload_json,
// not in D1, not anywhere. The PDF renderer scales PER AXIS
// (`sx, sy = self.width / _SIG_W, self.height / _SIG_H`), so it cannot undo an
// anisotropic capture.
//
// Consequence: the CAPTURE surface must be EXACTLY 600:180. `toLocal` normalises
// x and y independently against the surface's own rect, so a surface of any other
// shape bakes a permanent stretch into the stored signature — and the read-only
// preview below re-normalises it back into a 600:180 viewBox, so the distortion is
// INVISIBLE on screen and surfaces only in the filed legal PDF. That is why the
// sheet LETTERBOXES its canvas instead of filling the viewport.
// ─────────────────────────────────────────────────────────────────────────────

interface SignatureSheetProps {
  width: number;
  height: number;
  strokeWidth: number;
  /** Committed strokes seeded as the draft, so re-opening EDITS rather than restarts. */
  initialPaths: string[];
  onCommit: (paths: string[]) => void;
  onCancel: () => void;
  /** Fired once, on the first COMPLETED stroke of this sheet session. */
  onDraftDirty?: () => void;
}

/**
 * Full-screen capture sheet. Portaled to document.body: one live SignaturePad mount
 * point (the Form Editor's `.form-editor__preview-pane`) is `position: sticky`,
 * which creates a stacking context that would trap a `position: fixed` child
 * rendered in place.
 */
function SignatureSheet({
  width,
  height,
  strokeWidth,
  initialPaths,
  onCommit,
  onCancel,
  onDraftDirty,
}: SignatureSheetProps) {
  const [paths, setPaths] = useState<string[]>(initialPaths);
  const currentRef = useRef<string>(""); // in-progress `d`
  const drawingRef = useRef(false);
  // The pointer that owns the in-flight stroke. Web content gets NO palm rejection:
  // iOS delivers every simultaneous touch as its own pointer, and touch-action:none
  // guarantees each one lands here rather than being eaten as a gesture. On the old
  // ~350x105 strip a resting hand was unlikely; on this sheet's near-full-width
  // canvas it is the normal posture, and without this an unrelated contact would
  // overwrite currentRef and re-anchor the signature at the palm — silently, since
  // the preview re-renders from the same corrupted ref and agrees with the PDF.
  const activePointerRef = useRef<number | null>(null);
  /** Latch so onDraftDirty fires once per sheet session, not once per stroke. */
  const notifiedRef = useRef(false);
  const svgRef = useRef<SVGSVGElement>(null);
  const canvasRef = useRef<HTMLDivElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const [, force] = useState(0);

  const toLocal = useCallback(
    (clientX: number, clientY: number) => {
      const svg = svgRef.current;
      if (!svg) return { x: 0, y: 0 };
      const r = svg.getBoundingClientRect();
      // A zero-sized rect (element not laid out yet) would divide by zero and emit
      // Infinity into the path — which reaches reportlab verbatim. Refuse instead.
      if (r.width === 0 || r.height === 0) return { x: 0, y: 0 };
      // Clamp into the viewBox. setPointerCapture keeps delivering moves after the
      // finger leaves the letterboxed canvas, and the sheet surrounds that canvas
      // with inert margin, so overshoot is easy — unclamped, the ink renders
      // OUTSIDE the signature box in the PDF (reportlab does not clip the path).
      const x = Math.min(Math.max(((clientX - r.left) / r.width) * width, 0), width);
      const y = Math.min(Math.max(((clientY - r.top) / r.height) * height, 0), height);
      return { x: Math.round(x * 100) / 100, y: Math.round(y * 100) / 100 };
    },
    [width, height],
  );

  // ── Body scroll lock ──────────────────────────────────────────────────────
  // MUST live in an effect cleanup, never in the Done/Cancel handlers: the host can
  // unmount the pad asynchronously (a focus/visibilitychange refetch tears the
  // signing panel down), and React removes the portal DOM but will NOT undo an
  // imperative body-style mutation — that would strand the whole app unscrollable
  // with no recovery but a reload.
  // `overflow: hidden` on <body> does not lock scroll in iOS Safari; the
  // position:fixed + negative-top recipe does. <body> carries no explicit width, so
  // a fixed body would shrink-to-fit and the page behind would visibly reflow —
  // hence left/right/width are pinned too, and scrollY is restored on unlock.
  useLayoutEffect(() => {
    const body = document.body;
    const scrollY = window.scrollY;
    const prev = {
      position: body.style.position,
      top: body.style.top,
      left: body.style.left,
      right: body.style.right,
      width: body.style.width,
    };
    body.style.position = "fixed";
    body.style.top = `-${scrollY}px`;
    body.style.left = "0";
    body.style.right = "0";
    body.style.width = "100%";
    return () => {
      body.style.position = prev.position;
      body.style.top = prev.top;
      body.style.left = prev.left;
      body.style.right = prev.right;
      body.style.width = prev.width;
      window.scrollTo(0, scrollY);
    };
  }, []);

  // Focus into the sheet on open (the parent returns it to the trigger on close).
  useEffect(() => {
    rootRef.current?.focus();
  }, []);

  // Escape = Cancel, plus a Tab cycle confined to the sheet. Listening on document
  // rather than on the root: drawing on the (non-focusable) canvas can leave
  // document.activeElement on <body>, from which a keydown would never bubble
  // through the sheet. The trap is also what keeps the page behind unreachable —
  // My Tasks toggles `hidden` on its tab panels rather than unmounting them, so a
  // Tab that escaped could switch tabs while the sheet stayed painted over them.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onCancel();
        return;
      }
      if (e.key !== "Tab") return;
      const root = rootRef.current;
      if (!root) return;
      const nodes = Array.from(root.querySelectorAll<HTMLElement>("button:not([disabled])"));
      if (nodes.length === 0) return;
      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      const active = document.activeElement;
      // `active === root` is the OPEN state (the dialog itself holds focus) and must
      // be treated as a boundary: root.contains(root) is true, so without this the
      // very first Shift+Tab fell through to the browser default and walked focus
      // BACKWARDS out of the dialog onto the covered page — invisibly, since the
      // sheet is opaque and the body is position:fixed so it cannot even scroll the
      // focused control into view. A field PM could then Enter on the form's Submit
      // button and file the report with the signature still empty.
      if (!(active instanceof Node) || !root.contains(active) || active === root) {
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
      } else if (e.shiftKey && active === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onCancel]);

  // Belt-and-braces against the page panning under the finger. `touch-action: none`
  // on the canvas WRAPPER — a block-level <div>, because WebKit does not reliably
  // honour touch-action on an <svg>, which is the entire root cause of this defect —
  // is the primary control. React's synthetic onPointerMove cannot guarantee a
  // NON-PASSIVE listener, so preventDefault() there is not sufficient on its own.
  useEffect(() => {
    const el = canvasRef.current;
    if (!el) return;
    const onTouchMove = (e: TouchEvent) => {
      if (drawingRef.current) e.preventDefault();
    };
    el.addEventListener("touchmove", onTouchMove, { passive: false });
    return () => el.removeEventListener("touchmove", onTouchMove);
  }, []);

  const onPointerDown = useCallback(
    (e: ReactPointerEvent<SVGSVGElement>) => {
      e.preventDefault();
      // Ignore any contact that arrives while a stroke is already in flight (palm,
      // second finger). Gated on drawingRef rather than activePointerRef so it
      // self-heals: up/cancel always clears drawingRef, so a lost pointerup can
      // never wedge the pad.
      if (drawingRef.current) return;
      activePointerRef.current = e.pointerId;
      // Keep receiving move/up if the finger/stylus drifts outside the surface.
      // setPointerCapture can throw (detached element / released pointer) — never
      // let that abort the stroke.
      try {
        // currentTarget (the <svg> the listeners live on) — deterministic even if a
        // new stroke starts atop an already-drawn <path>.
        e.currentTarget.setPointerCapture(e.pointerId);
      } catch {
        /* non-fatal */
      }
      drawingRef.current = true;
      const { x, y } = toLocal(e.clientX, e.clientY);
      currentRef.current = `M ${x} ${y}`;
      force((n) => n + 1);
    },
    [toLocal],
  );

  const onPointerMove = useCallback(
    (e: ReactPointerEvent<SVGSVGElement>) => {
      if (!drawingRef.current || e.pointerId !== activePointerRef.current) return;
      e.preventDefault();
      const native = e.nativeEvent;
      // getCoalescedEvents() yields the high-frequency points batched into one move
      // (smooth strokes on fast devices). It can legitimately return [] — fall back
      // to the event itself so no point is ever dropped.
      const coalesced =
        typeof native.getCoalescedEvents === "function" ? native.getCoalescedEvents() : [];
      const events = coalesced.length > 0 ? coalesced : [native];
      let d = currentRef.current;
      for (const pe of events) {
        const { x, y } = toLocal(pe.clientX, pe.clientY);
        d += ` L ${x} ${y}`;
      }
      currentRef.current = d;
      force((n) => n + 1);
    },
    [toLocal],
  );

  // NOTE: deliberately NOT bound to onPointerLeave (the pre-sheet component was).
  // Pointer capture is meant to keep a stroke alive past the edge, but
  // setPointerCapture is best-effort — see the try/catch — so on a phone a finger
  // drifting off the small strip terminated the stroke early. Up + Cancel are the
  // real stroke terminators.
  const endStroke = useCallback(
    (e: ReactPointerEvent<SVGSVGElement>) => {
      // Only the pointer that started the stroke may end it — otherwise a palm lifting
      // first would commit the fragment and strand the still-drawing finger.
      if (!drawingRef.current || e.pointerId !== activePointerRef.current) return;
      e.preventDefault();
      drawingRef.current = false;
      activePointerRef.current = null;
      const finished = currentRef.current;
      currentRef.current = "";
      if (finished.includes("L")) {
        // ignore stray taps with no movement
        setPaths((prev) => [...prev, finished]);
        // First REAL stroke of this session arms the host's unsaved-work guard. Latched
        // so a multi-stroke signature notifies once, and gated on the same
        // movement check so a stray tap never marks the form dirty.
        if (!notifiedRef.current) {
          notifiedRef.current = true;
          onDraftDirty?.();
        }
      }
      force((n) => n + 1);
    },
    [onDraftDirty],
  );

  /** Clears the DRAFT only — the committed value is untouched until Done. */
  const clearDraft = useCallback(() => {
    setPaths([]);
    currentRef.current = "";
    drawingRef.current = false;
    activePointerRef.current = null;
    // Clear disables itself once the draft is empty, which would drop focus to
    // <body>: no ring, nothing announced, and the next keypress does nothing inside
    // a full-screen overlay. Park focus back on the dialog so the trap still owns it.
    rootRef.current?.focus();
    force((n) => n + 1);
  }, []);

  const combined = [...paths, currentRef.current].filter(Boolean).join(" ");
  const isEmpty = combined.length === 0;

  return (
    <div
      className="sigsheet"
      role="dialog"
      aria-modal="true"
      aria-label="Sign"
      ref={rootRef}
      tabIndex={-1}
    >
      <div className="sigsheet__head">
        <span className="sigsheet__title">Sign</span>
        <span className="sigsheet__rotate">Rotate for a larger signing area</span>
      </div>

      <div className="sigsheet__body">
        {/* touch-action: none lives HERE, on the block-level wrapper — see above. */}
        <div className="sigsheet__canvas" ref={canvasRef}>
          <svg
            ref={svgRef}
            className="sigsheet__surface"
            viewBox={`0 0 ${width} ${height}`}
            style={{ aspectRatio: `${width} / ${height}` }}
            role="img"
            aria-label="Signature drawing surface"
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={endStroke}
            onPointerCancel={endStroke}
          >
            <path
              d={combined}
              fill="none"
              stroke="#0e0e0e"
              strokeWidth={strokeWidth}
              strokeLinecap="round"
              strokeLinejoin="round"
              vectorEffect="non-scaling-stroke"
            />
          </svg>
        </div>
      </div>

      <div className="sigsheet__foot">
        <button type="button" className="btn btn--secondary" onClick={clearDraft} disabled={isEmpty}>
          Clear
        </button>
        <span className="sigsheet__spacer" />
        <button type="button" className="btn btn--secondary" onClick={onCancel}>
          Cancel
        </button>
        <button type="button" className="btn btn--primary" onClick={() => onCommit(paths)}>
          Done
        </button>
      </div>
    </div>
  );
}

/**
 * On-screen signature capture that emits TRUE SVG path data (vector), not raster.
 * The mission (§7) requires SVG path data; canvas libs (signature_pad,
 * react-signature-canvas) export raster PNG and do not satisfy it. Hand-rolled over
 * Pointer Events so it owns the exact `d` string and works on phones/tablets.
 *
 * The inline element is a read-only PREVIEW and a tap target; capture happens in the
 * full-screen sheet above. iOS Safari does not reliably honour `touch-action` on an
 * <svg>, so the old inline surface let the page pan under the finger mid-stroke —
 * breaking strokes on exactly the phones and tablets the field PMs sign on.
 */
export function SignaturePad({
  width = 600,
  height = 180,
  strokeWidth = 2.5,
  value,
  onChange,
  onDraftDirty,
}: SignaturePadProps) {
  // CONTROLLED when `value` is a string (including ""), uncontrolled when undefined —
  // the standard <input value>/defaultValue duality. A mount-time seed would NOT be
  // enough: an amend rewrites form values WITHOUT remounting the pad, which is exactly
  // the path that filed the old signature under a blank-looking preview.
  const controlled = value !== undefined;
  const [internal, setInternal] = useState(""); // uncontrolled mode only
  const committed = controlled ? value : internal;
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const wasOpenRef = useRef(false);

  // Return focus to the trigger when the sheet closes — it is now the ONLY route to
  // capture, so dropping focus here strands a keyboard / screen-reader user.
  useEffect(() => {
    if (wasOpenRef.current && !open) triggerRef.current?.focus();
    wasOpenRef.current = open;
  }, [open]);

  const handleCommit = useCallback(
    (next: string[]) => {
      const d = next.join(" ");
      if (!controlled) setInternal(d);
      onChange?.(d, d.length === 0);
      setOpen(false);
    },
    [controlled, onChange],
  );

  const handleCancel = useCallback(() => setOpen(false), []);

  const clear = useCallback(() => {
    if (!controlled) setInternal("");
    onChange?.("", true);
  }, [controlled, onChange]);

  const isEmpty = committed.length === 0;

  return (
    <div className="sig">
      {/* A real <button>: the preview is the only route to signing, so it must be
          keyboard- and screen-reader-reachable. Its accessible name is set
          explicitly so it does not inherit the inner svg's. */}
      <button
        ref={triggerRef}
        type="button"
        className="sig__trigger"
        aria-label="Open signature pad"
        onClick={() => setOpen(true)}
      >
        {/* role="img" + this exact aria-label are a TEST CONTRACT: render-smoke counts
            one svg[role="img"] per pad across every active form. Keep them. */}
        <svg
          className="sig__surface"
          viewBox={`0 0 ${width} ${height}`}
          style={{ aspectRatio: `${width} / ${height}` }}
          role="img"
          aria-label="Signature capture area"
        >
          <path
            d={committed}
            fill="none"
            stroke="#0e0e0e"
            strokeWidth={strokeWidth}
            strokeLinecap="round"
            strokeLinejoin="round"
            vectorEffect="non-scaling-stroke"
          />
        </svg>
      </button>
      <div className="sig__bar">
        <span className="sig__hint">{isEmpty ? "Tap to sign" : "Tap to edit"}</span>
        <button type="button" className="btn btn--secondary" onClick={clear} disabled={isEmpty}>
          Clear
        </button>
      </div>
      {open
        ? createPortal(
            <SignatureSheet
              width={width}
              height={height}
              strokeWidth={strokeWidth}
              /* LOAD-BEARING: the sheet seeds from the SAME resolved value as the
                 preview. Controlling only the preview would let the user tap a row
                 showing person B's ink and open a sheet holding person A's — Done
                 would then write A's signature onto B's row. */
              initialPaths={committed ? [committed] : []}
              onCommit={handleCommit}
              onCancel={handleCancel}
              onDraftDirty={onDraftDirty}
            />,
            document.body,
          )
        : null}
    </div>
  );
}
