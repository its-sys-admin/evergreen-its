import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { PointerEvent as ReactPointerEvent } from "react";

interface SignaturePadProps {
  width?: number;
  height?: number;
  strokeWidth?: number;
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
}: SignatureSheetProps) {
  const [paths, setPaths] = useState<string[]>(initialPaths);
  const currentRef = useRef<string>(""); // in-progress `d`
  const drawingRef = useRef(false);
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
      if (!(active instanceof Node) || !root.contains(active)) {
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
      if (!drawingRef.current) return;
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
  const endStroke = useCallback((e: ReactPointerEvent<SVGSVGElement>) => {
    if (!drawingRef.current) return;
    e.preventDefault();
    drawingRef.current = false;
    const finished = currentRef.current;
    currentRef.current = "";
    if (finished.includes("L")) {
      // ignore stray taps with no movement
      setPaths((prev) => [...prev, finished]);
    }
    force((n) => n + 1);
  }, []);

  /** Clears the DRAFT only — the committed value is untouched until Done. */
  const clearDraft = useCallback(() => {
    setPaths([]);
    currentRef.current = "";
    drawingRef.current = false;
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
  onChange,
}: SignaturePadProps) {
  const [paths, setPaths] = useState<string[]>([]); // each committed stroke = one `d`
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
      setPaths(next);
      const d = next.join(" ");
      onChange?.(d, d.length === 0);
      setOpen(false);
    },
    [onChange],
  );

  const handleCancel = useCallback(() => setOpen(false), []);

  const clear = useCallback(() => {
    setPaths([]);
    onChange?.("", true);
  }, [onChange]);

  const combined = paths.join(" ");
  const isEmpty = combined.length === 0;

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
            d={combined}
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
              initialPaths={paths}
              onCommit={handleCommit}
              onCancel={handleCancel}
            />,
            document.body,
          )
        : null}
    </div>
  );
}
