import { useRef, useState, type ReactNode } from "react";

// GridViewport — a sticky-header scroll box whose height the OPERATOR sets. Wraps the two
// big review grids (manifest rows, extracted estimate lines) that previously scrolled
// inside a fixed inline `maxHeight: 28rem` (or none at all): the column labels now pin to
// the top of the scroller, and the height is adjustable via a drag handle plus
// Compact/Tall/Max presets, persisted per browser in localStorage.
//
// The non-obvious constraints:
//   • Height is applied as maxHeight, NOT height — a short table stays content-height
//     instead of dragging an empty scroll gutter below its last row.
//   • Persistence writes on pointerup/pointercancel, preset clicks, and handled keydowns
//     ONLY — never per pointermove. A drag emits dozens of moves per second; localStorage
//     is synchronous and per-frame writes would jank the very interaction they record.
//   • PERF CONTRACT: height state lives HERE and `children` is referentially stable
//     across our re-renders, so React bails out of reconciling the (potentially
//     200-col × 1000+-row) table subtree on drag frames. Never clone/augment children.
//   • localStorage is untrusted config: reads and writes are try/catch'd (private mode,
//     storage quota) and junk values fall back to the compact default, never throw.

const KEY_PREFIX = "its-portal-gridvp:v1:";
const PRESET_PX = { compact: 448, tall: 704 } as const; // compact = the old inline 28rem
const MAX_CUSTOM = 1600;
const DEFAULT_MIN_PX = 160;

type Height =
  | { kind: "compact" }
  | { kind: "tall" }
  | { kind: "max" } // CSS class carries max(320px, calc(100dvh - 240px))
  | { kind: "custom"; px: number };

function clamp(px: number, minPx: number): number {
  return Math.min(MAX_CUSTOM, Math.max(minPx, Math.round(px)));
}

/** Stored value is a preset name or a bare integer string WITHIN [minPx, MAX_CUSTOM];
 *  anything else — junk, a negative, or an integer OUTSIDE those bounds — is discarded
 *  to the compact default. */
function loadHeight(storageKey: string, minPx: number): Height {
  try {
    const raw = localStorage.getItem(KEY_PREFIX + storageKey);
    if (raw === "compact" || raw === "tall" || raw === "max") return { kind: raw };
    if (raw !== null && /^\d+$/.test(raw)) {
      const px = parseInt(raw, 10);
      if (px >= minPx && px <= MAX_CUSTOM) return { kind: "custom", px };
    }
  } catch {
    /* storage unavailable — the default is fine */
  }
  return { kind: "compact" };
}

/** The height as a number: presets from the table, custom as-is, "max" treated as the
 *  ceiling so arrow keys from the max preset start somewhere sensible + consistent. */
function numericPx(h: Height): number {
  if (h.kind === "custom") return h.px;
  if (h.kind === "max") return MAX_CUSTOM;
  return PRESET_PX[h.kind];
}

export function GridViewport({
  storageKey,
  label,
  minPx = DEFAULT_MIN_PX,
  children,
}: {
  storageKey: string;
  label: string;
  minPx?: number;
  children: ReactNode;
}) {
  const [height, setHeight] = useState<Height>(() => loadHeight(storageKey, minPx));
  const scrollRef = useRef<HTMLDivElement | null>(null);
  // Latest height for the pointerup save — the drag's moves update state without
  // persisting, so the up handler must read the CURRENT value, not its render closure.
  const heightRef = useRef(height);
  heightRef.current = height;
  const dragRef = useRef<{ startY: number; startH: number } | null>(null);

  function save(h: Height): void {
    try {
      localStorage.setItem(KEY_PREFIX + storageKey, h.kind === "custom" ? String(h.px) : h.kind);
    } catch {
      /* storage unavailable — the height still applied for this session */
    }
  }

  function apply(h: Height): void {
    setHeight(h);
    save(h);
  }

  function onPointerDown(e: React.PointerEvent<HTMLDivElement>): void {
    try {
      // Optional-chained AND fenced: jsdom either lacks setPointerCapture or throws on
      // a pointer it never registered; a real browser needs it so the drag survives
      // leaving the handle.
      e.currentTarget.setPointerCapture?.(e.pointerId);
    } catch {
      /* capture is an enhancement, not a requirement */
    }
    dragRef.current = {
      startY: e.clientY,
      // The rendered edge when we have one; jsdom rects are 0 and a content-limited box
      // should drag from where it actually sits, so the `||` fallback is load-bearing.
      startH: scrollRef.current?.getBoundingClientRect().height || numericPx(height),
    };
  }

  function onPointerMove(e: React.PointerEvent<HTMLDivElement>): void {
    const drag = dragRef.current;
    if (drag === null) return;
    setHeight({ kind: "custom", px: clamp(drag.startH + (e.clientY - drag.startY), minPx) });
  }

  function onPointerEnd(): void {
    if (dragRef.current === null) return;
    dragRef.current = null;
    save(heightRef.current);
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLDivElement>): void {
    const from = numericPx(height);
    let next: Height | null = null;
    if (e.key === "ArrowDown") next = { kind: "custom", px: clamp(from + 16, minPx) }; // Down = taller
    else if (e.key === "ArrowUp") next = { kind: "custom", px: clamp(from - 16, minPx) };
    else if (e.key === "PageDown") next = { kind: "custom", px: clamp(from + 128, minPx) };
    else if (e.key === "PageUp") next = { kind: "custom", px: clamp(from - 128, minPx) };
    else if (e.key === "Home") next = { kind: "custom", px: minPx };
    else if (e.key === "End") next = { kind: "max" };
    if (next !== null) {
      e.preventDefault();
      apply(next);
    }
  }

  const isMax = height.kind === "max";
  return (
    <div className="gridvp">
      <div
        ref={scrollRef}
        // gridvp__scroll stays the LEADING literal here — the Python CSS-class guard's
        // regex captures the first literal segment of a template className.
        className={`gridvp__scroll ${isMax ? "gridvp__scroll--max" : ""}`}
        style={isMax ? undefined : { maxHeight: numericPx(height) }}
      >
        {children}
      </div>
      <div className="gridvp__bar">
        <div
          className="gridvp__handle"
          role="separator"
          aria-orientation="horizontal"
          aria-label={`Resize ${label}`}
          aria-valuemin={minPx}
          aria-valuemax={MAX_CUSTOM}
          aria-valuenow={numericPx(height)}
          tabIndex={0}
          onKeyDown={onKeyDown}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerEnd}
          onPointerCancel={onPointerEnd}
        >
          <span className="gridvp__grip" aria-hidden="true" />
        </div>
        <div className="gridvp__presets" role="group" aria-label={`${label} height`}>
          <button
            type="button"
            className="btn btn--secondary btn--sm gridvp__preset"
            aria-pressed={height.kind === "compact"}
            onClick={() => apply({ kind: "compact" })}
          >
            Compact
          </button>
          <button
            type="button"
            className="btn btn--secondary btn--sm gridvp__preset"
            aria-pressed={height.kind === "tall"}
            onClick={() => apply({ kind: "tall" })}
          >
            Tall
          </button>
          <button
            type="button"
            className="btn btn--secondary btn--sm gridvp__preset"
            aria-pressed={height.kind === "max"}
            onClick={() => apply({ kind: "max" })}
          >
            Max
          </button>
        </div>
      </div>
    </div>
  );
}
