/**
 * SignaturePad — full-screen capture sheet.
 *
 * Capture moved off the inline strip (iOS Safari does not reliably honour
 * `touch-action` on an <svg>, so the page panned under the finger mid-stroke) into
 * a sheet portaled to document.body. These tests pin the parts jsdom CAN prove:
 * the open/close state machine, the commit-vs-discard contract, the body scroll
 * lock's full restoration, and the coordinate-space invariant.
 *
 * What jsdom CANNOT prove — that the page no longer scrolls under a real finger —
 * is device verification, not a unit test. See docs: this suite is the regression
 * net around the contract, not the fix itself.
 */
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SignaturePad } from "../SignaturePad";

// jsdom's window.scrollTo is a not-implemented stub; the scroll lock calls it on
// restore. Replaced per-test where the call itself is the assertion.
window.scrollTo = () => {};

afterEach(() => {
  cleanup();
  // cleanup() unmounts roots but does NOT reset document.body.style — a leaked lock
  // would silently contaminate every later test in this file.
  document.body.removeAttribute("style");
});

beforeEach(() => {
  Object.defineProperty(window, "scrollY", { value: 0, configurable: true, writable: true });
});

// ── helpers ──────────────────────────────────────────────────────────────────

/** The sheet is portaled to document.body, so it is NEVER inside RTL's container. */
function sheet(): HTMLElement | null {
  return document.body.querySelector('[role="dialog"][aria-label="Sign"]');
}

function openSheet(container: HTMLElement): HTMLElement {
  fireEvent.click(container.querySelector('[aria-label="Open signature pad"]') as HTMLElement);
  const s = sheet();
  if (!s) throw new Error("signature sheet did not open");
  return s;
}

function surface(): SVGSVGElement {
  const s = sheet();
  if (!s) throw new Error("signature sheet is not open");
  return s.querySelector('[aria-label="Signature drawing surface"]') as SVGSVGElement;
}

function sheetButton(name: string): HTMLButtonElement {
  const s = sheet();
  if (!s) throw new Error("signature sheet is not open");
  const btn = Array.from(s.querySelectorAll("button")).find(
    (b) => b.textContent?.trim() === name,
  );
  if (!btn) throw new Error(`signature sheet has no "${name}" button`);
  return btn;
}

/** jsdom has no layout engine — every rect is 0x0 unless stubbed. */
function mockRect(el: Element, box: { left?: number; top?: number; width: number; height: number }) {
  const left = box.left ?? 0;
  const top = box.top ?? 0;
  el.getBoundingClientRect = () =>
    ({
      x: left,
      y: top,
      left,
      top,
      right: left + box.width,
      bottom: top + box.height,
      width: box.width,
      height: box.height,
    }) as DOMRect;
}

function draw(el: SVGSVGElement, points: Array<[number, number]>) {
  const [first, ...rest] = points;
  fireEvent.pointerDown(el, { clientX: first[0], clientY: first[1], pointerId: 1 });
  for (const [x, y] of rest) fireEvent.pointerMove(el, { clientX: x, clientY: y, pointerId: 1 });
  const last = points[points.length - 1];
  fireEvent.pointerUp(el, { clientX: last[0], clientY: last[1], pointerId: 1 });
}

/** Pull every numeric coordinate out of an SVG path `d`. */
function coords(d: string): Array<[number, number]> {
  const nums = (d.match(/-?\d+(?:\.\d+)?/g) ?? []).map(Number);
  const out: Array<[number, number]> = [];
  for (let i = 0; i + 1 < nums.length; i += 2) out.push([nums[i], nums[i + 1]]);
  return out;
}

// ── open / close state machine ───────────────────────────────────────────────

describe("SignaturePad — sheet open/close", () => {
  it("is closed initially and opens on an inline tap", () => {
    const { container } = render(<SignaturePad />);
    expect(sheet()).toBeNull();
    openSheet(container);
    expect(sheet()).not.toBeNull();
  });

  it("closes on Done", () => {
    const { container } = render(<SignaturePad />);
    openSheet(container);
    fireEvent.click(sheetButton("Done"));
    expect(sheet()).toBeNull();
  });

  it("closes on Cancel", () => {
    const { container } = render(<SignaturePad />);
    openSheet(container);
    fireEvent.click(sheetButton("Cancel"));
    expect(sheet()).toBeNull();
  });

  it("closes on Escape", () => {
    const { container } = render(<SignaturePad />);
    openSheet(container);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(sheet()).toBeNull();
  });

  it("returns focus to the inline trigger on close — it is the ONLY route to capture", () => {
    const { container } = render(<SignaturePad />);
    const trigger = container.querySelector('[aria-label="Open signature pad"]') as HTMLElement;
    openSheet(container);
    fireEvent.click(sheetButton("Cancel"));
    expect(document.activeElement).toBe(trigger);
  });
});

// ── commit vs discard ────────────────────────────────────────────────────────

describe("SignaturePad — commit semantics", () => {
  it("does NOT fire onChange while drawing — only on Done", () => {
    const onChange = vi.fn();
    const { container } = render(<SignaturePad onChange={onChange} />);
    openSheet(container);
    mockRect(surface(), { width: 600, height: 180 });

    draw(surface(), [
      [10, 10],
      [20, 24],
      [30, 40],
    ]);
    expect(onChange).not.toHaveBeenCalled();

    fireEvent.click(sheetButton("Done"));
    expect(onChange).toHaveBeenCalledTimes(1);
    const [d, isEmpty] = onChange.mock.calls[0];
    expect(isEmpty).toBe(false);
    expect(d).toMatch(/^M [\d.]+ [\d.]+ L /);
  });

  it("Cancel discards — the parent is never told, and the preview keeps the prior value", () => {
    const onChange = vi.fn();
    const { container } = render(<SignaturePad onChange={onChange} />);

    // Commit a first signature.
    openSheet(container);
    mockRect(surface(), { width: 600, height: 180 });
    draw(surface(), [
      [10, 10],
      [20, 24],
    ]);
    fireEvent.click(sheetButton("Done"));
    const committed = onChange.mock.calls[0][0] as string;
    onChange.mockClear();

    // Re-open, scribble more, then Cancel.
    openSheet(container);
    mockRect(surface(), { width: 600, height: 180 });
    draw(surface(), [
      [100, 100],
      [200, 150],
    ]);
    fireEvent.click(sheetButton("Cancel"));

    expect(onChange).not.toHaveBeenCalled();
    const preview = container.querySelector('svg[role="img"] path') as SVGPathElement;
    expect(preview.getAttribute("d")).toBe(committed);
  });

  it("re-opening seeds the draft from the committed strokes (edit, not restart)", () => {
    const onChange = vi.fn();
    const { container } = render(<SignaturePad onChange={onChange} />);

    openSheet(container);
    mockRect(surface(), { width: 600, height: 180 });
    draw(surface(), [
      [10, 10],
      [20, 24],
    ]);
    fireEvent.click(sheetButton("Done"));
    const committed = onChange.mock.calls[0][0] as string;

    openSheet(container);
    const drafted = (surface().querySelector("path") as SVGPathElement).getAttribute("d");
    expect(drafted).toBe(committed);
  });

  it("Done after clearing the draft commits an EMPTY signature", () => {
    const onChange = vi.fn();
    const { container } = render(<SignaturePad onChange={onChange} />);

    openSheet(container);
    mockRect(surface(), { width: 600, height: 180 });
    draw(surface(), [
      [10, 10],
      [20, 24],
    ]);
    fireEvent.click(sheetButton("Done"));
    onChange.mockClear();

    openSheet(container);
    fireEvent.click(sheetButton("Clear"));
    fireEvent.click(sheetButton("Done"));

    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange.mock.calls[0]).toEqual(["", true]);
  });

  it("the inline Clear still clears a committed signature — unchanged contract", () => {
    const onChange = vi.fn();
    const { container } = render(<SignaturePad onChange={onChange} />);

    openSheet(container);
    mockRect(surface(), { width: 600, height: 180 });
    draw(surface(), [
      [10, 10],
      [20, 24],
    ]);
    fireEvent.click(sheetButton("Done"));
    onChange.mockClear();

    const clear = Array.from(container.querySelectorAll("button")).find(
      (b) => b.textContent?.trim() === "Clear",
    ) as HTMLButtonElement;
    expect(clear.disabled).toBe(false);
    fireEvent.click(clear);
    expect(onChange).toHaveBeenCalledWith("", true);
  });
});

// ── coordinate space (the contract with form_pdf.py) ─────────────────────────

describe("SignaturePad — 600x180 coordinate space", () => {
  it("maps into 0..600 x 0..180 regardless of the surface's on-screen size", () => {
    for (const scale of [0.5, 1, 2, 4]) {
      const onChange = vi.fn();
      const { container, unmount } = render(<SignaturePad onChange={onChange} />);
      openSheet(container);
      mockRect(surface(), { width: 600 * scale, height: 180 * scale });

      // Draw the same PROPORTIONAL stroke at every scale: quarter → half → three-quarters.
      draw(surface(), [
        [150 * scale, 45 * scale],
        [300 * scale, 90 * scale],
        [450 * scale, 135 * scale],
      ]);
      fireEvent.click(sheetButton("Done"));

      const d = onChange.mock.calls[0][0] as string;
      expect(coords(d), `scale ${scale}`).toEqual([
        [150, 45],
        [300, 90],
        [450, 135],
      ]);
      unmount();
      document.body.removeAttribute("style");
    }
  });

  it("honours the surface's offset — coordinates are relative to the canvas, not the page", () => {
    const onChange = vi.fn();
    const { container } = render(<SignaturePad onChange={onChange} />);
    openSheet(container);
    mockRect(surface(), { left: 100, top: 40, width: 600, height: 180 });

    draw(surface(), [
      [100, 40],
      [400, 130],
    ]);
    fireEvent.click(sheetButton("Done"));

    expect(coords(onChange.mock.calls[0][0] as string)).toEqual([
      [0, 0],
      [300, 90],
    ]);
  });

  it("clamps overshoot into the viewBox — unclamped ink renders OUTSIDE the PDF's signature box", () => {
    const onChange = vi.fn();
    const { container } = render(<SignaturePad onChange={onChange} />);
    openSheet(container);
    mockRect(surface(), { width: 600, height: 180 });

    // Pointer capture keeps delivering moves after the finger leaves the canvas.
    draw(surface(), [
      [-500, -500],
      [300, 90],
      [5000, 5000],
    ]);
    fireEvent.click(sheetButton("Done"));

    expect(coords(onChange.mock.calls[0][0] as string)).toEqual([
      [0, 0],
      [300, 90],
      [600, 180],
    ]);
  });

  it("refuses a zero-sized rect rather than dividing by it", () => {
    const onChange = vi.fn();
    const { container } = render(<SignaturePad onChange={onChange} />);
    openSheet(container);
    // No mockRect — jsdom's native all-zero rect.
    draw(surface(), [
      [10, 10],
      [20, 24],
    ]);
    fireEvent.click(sheetButton("Done"));

    const d = onChange.mock.calls[0][0] as string;
    expect(d).not.toMatch(/Infinity|NaN/);
    // Pin the GUARD, not the clamp. Without the early return the division yields
    // Infinity, which the clamp would quietly pin to the far corner (600,180) — a
    // bottom-right dot that still reads as "signed". Asserting only "no Infinity"
    // would pass in that world, so assert the guard's actual contract.
    expect(coords(d).every(([x, y]) => x === 0 && y === 0)).toBe(true);
  });
});

// ── concurrent contacts ──────────────────────────────────────────────────────

describe("SignaturePad — multi-touch", () => {
  it("ignores a resting palm / second finger instead of re-anchoring the signature", () => {
    // Web content gets NO palm rejection: iOS delivers every simultaneous touch as
    // its own pointer. On the old small strip this was unlikely; on a near-full-width
    // canvas a resting hand is the normal posture.
    const onChange = vi.fn();
    const { container } = render(<SignaturePad onChange={onChange} />);
    openSheet(container);
    const pad = surface();
    mockRect(pad, { width: 600, height: 180 });

    fireEvent.pointerDown(pad, { clientX: 10, clientY: 90, pointerId: 1 });
    fireEvent.pointerMove(pad, { clientX: 60, clientY: 40, pointerId: 1 });
    // the palm lands mid-signature and drags a little
    fireEvent.pointerDown(pad, { clientX: 500, clientY: 170, pointerId: 2 });
    fireEvent.pointerMove(pad, { clientX: 520, clientY: 175, pointerId: 2 });
    // the writing finger carries on, then both lift
    fireEvent.pointerMove(pad, { clientX: 110, clientY: 140, pointerId: 1 });
    fireEvent.pointerUp(pad, { clientX: 110, clientY: 140, pointerId: 1 });
    fireEvent.pointerUp(pad, { clientX: 520, clientY: 175, pointerId: 2 });
    fireEvent.click(sheetButton("Done"));

    expect(onChange.mock.calls[0][0]).toBe("M 10 90 L 60 40 L 110 140");
  });

  it("a lost pointerup cannot wedge the pad — the next contact starts a fresh stroke", () => {
    const onChange = vi.fn();
    const { container } = render(<SignaturePad onChange={onChange} />);
    openSheet(container);
    const pad = surface();
    mockRect(pad, { width: 600, height: 180 });

    fireEvent.pointerDown(pad, { clientX: 10, clientY: 90, pointerId: 1 });
    fireEvent.pointerMove(pad, { clientX: 60, clientY: 40, pointerId: 1 });
    fireEvent.pointerCancel(pad, { clientX: 60, clientY: 40, pointerId: 1 });

    fireEvent.pointerDown(pad, { clientX: 200, clientY: 20, pointerId: 7 });
    fireEvent.pointerMove(pad, { clientX: 260, clientY: 60, pointerId: 7 });
    fireEvent.pointerUp(pad, { clientX: 260, clientY: 60, pointerId: 7 });
    fireEvent.click(sheetButton("Done"));

    expect(onChange.mock.calls[0][0]).toContain("M 200 20 L 260 60");
  });
});

// ── the non-passive touchmove belt ───────────────────────────────────────────

describe("SignaturePad — touchmove suppression", () => {
  it("preventDefaults touchmove ONLY while a stroke is in flight", () => {
    // This is the commit's belt-and-braces against the page panning under the finger.
    // A React synthetic onTouchMove would be attached PASSIVELY, making
    // preventDefault a no-op — so this also pins the listener's non-passive-ness.
    const { container } = render(<SignaturePad />);
    const s = openSheet(container);
    mockRect(surface(), { width: 600, height: 180 });
    const canvas = s.querySelector(".sigsheet__canvas") as HTMLElement;

    const idle = new Event("touchmove", { bubbles: true, cancelable: true });
    canvas.dispatchEvent(idle);
    expect(idle.defaultPrevented, "an idle canvas must not block panning").toBe(false);

    fireEvent.pointerDown(surface(), { clientX: 10, clientY: 10, pointerId: 1 });
    const during = new Event("touchmove", { bubbles: true, cancelable: true });
    canvas.dispatchEvent(during);
    expect(during.defaultPrevented, "a stroke in flight must suppress the pan").toBe(true);
  });
});

// ── focus containment ────────────────────────────────────────────────────────

describe("SignaturePad — focus trap", () => {
  it("Shift+Tab as the FIRST keystroke wraps to the last control, not out of the dialog", () => {
    // Regression: root.contains(root) is true, so the open state fell through to the
    // browser default and walked focus backwards onto the covered page — invisibly.
    const { container } = render(<SignaturePad />);
    const s = openSheet(container);
    expect(document.activeElement).toBe(s);

    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(sheetButton("Done"));
  });

  it("Tab as the first keystroke moves to the first enabled control", () => {
    const { container } = render(<SignaturePad />);
    openSheet(container);
    // Clear is disabled while the draft is empty, so Cancel is first.
    fireEvent.keyDown(document, { key: "Tab" });
    expect(document.activeElement).toBe(sheetButton("Cancel"));
  });

  it("Tab wraps from the last control back to the first", () => {
    const { container } = render(<SignaturePad />);
    openSheet(container);
    sheetButton("Done").focus();
    fireEvent.keyDown(document, { key: "Tab" });
    expect(document.activeElement).toBe(sheetButton("Cancel"));
  });

  it("recaptures focus that drifted to <body> — drawing blurs onto a non-focusable svg", () => {
    const { container } = render(<SignaturePad />);
    const s = openSheet(container);
    (document.activeElement as HTMLElement | null)?.blur();

    fireEvent.keyDown(document, { key: "Tab" });
    expect(s.contains(document.activeElement)).toBe(true);
  });

  it("Clear parks focus back on the dialog instead of dropping it to <body>", () => {
    const onChange = vi.fn();
    const { container } = render(<SignaturePad onChange={onChange} />);
    const s = openSheet(container);
    mockRect(surface(), { width: 600, height: 180 });
    draw(surface(), [
      [10, 10],
      [20, 24],
    ]);

    const clear = sheetButton("Clear");
    clear.focus();
    fireEvent.click(clear);

    expect(clear.disabled, "Clear disables itself once the draft is empty").toBe(true);
    expect(document.activeElement).toBe(s);
  });
});

// ── body scroll lock ─────────────────────────────────────────────────────────

describe("SignaturePad — body scroll lock", () => {
  it("locks the body on open and fully restores it — including scroll offset — on close", () => {
    Object.defineProperty(window, "scrollY", { value: 250, configurable: true, writable: true });
    const scrollTo = vi.fn();
    window.scrollTo = scrollTo as unknown as typeof window.scrollTo;

    const { container } = render(<SignaturePad />);
    openSheet(container);

    // position:fixed alone collapses the body box (it has no explicit width), so the
    // page behind would visibly reflow — left/right/width are part of the recipe.
    expect(document.body.style.position).toBe("fixed");
    expect(document.body.style.top).toBe("-250px");
    expect(document.body.style.left).toBe("0px");
    expect(document.body.style.right).toBe("0px");
    expect(document.body.style.width).toBe("100%");

    fireEvent.click(sheetButton("Cancel"));

    expect(document.body.style.position).toBe("");
    expect(document.body.style.top).toBe("");
    expect(document.body.style.left).toBe("");
    expect(document.body.style.right).toBe("");
    expect(document.body.style.width).toBe("");
    expect(scrollTo).toHaveBeenCalledWith(0, 250);
  });

  it("restores the body when the HOST unmounts mid-signing — an async teardown must not strand the app", () => {
    // A focus/visibilitychange refetch can tear the signing panel down while the
    // sheet is open. React removes the portal DOM but would NOT undo an imperative
    // body-style mutation, leaving the whole app unscrollable until a reload.
    window.scrollTo = () => {};
    const { container, unmount } = render(<SignaturePad />);
    openSheet(container);
    expect(document.body.style.position).toBe("fixed");

    unmount();

    expect(document.body.style.position).toBe("");
    expect(document.body.style.top).toBe("");
  });
});

// ── the render-smoke contract ────────────────────────────────────────────────

describe("SignaturePad — inline preview contract", () => {
  it("keeps exactly one svg[role=img] inside the container, open or closed", () => {
    // render-smoke counts container.querySelectorAll('svg[role="img"]') once per pad
    // across every active form. The sheet lives under document.body, so it must never
    // perturb that count — and the preview must never stop rendering.
    const { container } = render(<SignaturePad />);
    expect(container.querySelectorAll('svg[role="img"]')).toHaveLength(1);

    openSheet(container);
    expect(container.querySelectorAll('svg[role="img"]')).toHaveLength(1);

    fireEvent.click(sheetButton("Cancel"));
    expect(container.querySelectorAll('svg[role="img"]')).toHaveLength(1);
  });

  it("keeps the preview's aria-label — the selector render-smoke and signOn rely on", () => {
    const { container } = render(<SignaturePad />);
    const preview = container.querySelector('svg[role="img"]') as SVGSVGElement;
    expect(preview.getAttribute("aria-label")).toBe("Signature capture area");
  });

  it("the capture surface carries a DISTINCT label so the two can never be confused", () => {
    const { container } = render(<SignaturePad />);
    openSheet(container);
    expect(surface().getAttribute("aria-label")).toBe("Signature drawing surface");
    expect(
      container.querySelector('[aria-label="Signature drawing surface"]'),
      "the capture surface must NOT be inside the container",
    ).toBeNull();
  });

  it("the hint tells the user the preview is tappable, and flips once signed", () => {
    const { container } = render(<SignaturePad />);
    expect(container.querySelector(".sig__hint")?.textContent).toBe("Tap to sign");

    openSheet(container);
    mockRect(surface(), { width: 600, height: 180 });
    draw(surface(), [
      [10, 10],
      [20, 24],
    ]);
    fireEvent.click(sheetButton("Done"));

    expect(container.querySelector(".sig__hint")?.textContent).toBe("Tap to edit");
  });
});

// ── controlled `value` ───────────────────────────────────────────────────────

describe("SignaturePad — controlled value", () => {
  const PRIOR = "M 10 20 L 30 40 L 60 25";

  it("renders the prop as the preview with ZERO interaction — the AMEND case", () => {
    // The bug: an amend repopulates form values WITHOUT remounting the pad, so an
    // uncontrolled pad showed blank over a real, submittable signature — and since the
    // inline element is a preview, blank reads as "not signed".
    const { container } = render(<SignaturePad value={PRIOR} />);
    const preview = container.querySelector('svg[role="img"] path') as SVGPathElement;
    expect(preview.getAttribute("d")).toBe(PRIOR);
    expect(container.querySelector(".sig__hint")?.textContent).toBe("Tap to edit");
  });

  it("seeds the SHEET from the prop too, not from internal state", () => {
    // Controlling only the preview would let a user tap a row showing person B's ink and
    // open a sheet holding person A's — Done would then write A's signature onto B's row.
    const onChange = vi.fn();
    const { container, rerender } = render(<SignaturePad value={PRIOR} onChange={onChange} />);

    // Commit something else while controlled; the parent then supplies a DIFFERENT value.
    openSheet(container);
    expect((surface().querySelector("path") as SVGPathElement).getAttribute("d")).toBe(PRIOR);
    fireEvent.click(sheetButton("Cancel"));

    const OTHER = "M 1 2 L 3 4";
    rerender(<SignaturePad value={OTHER} onChange={onChange} />);
    openSheet(container);
    expect((surface().querySelector("path") as SVGPathElement).getAttribute("d")).toBe(OTHER);
  });

  it("editing a controlled signature APPENDS — edit, not restart", () => {
    const onChange = vi.fn();
    const { container } = render(<SignaturePad value={PRIOR} onChange={onChange} />);
    openSheet(container);
    mockRect(surface(), { width: 600, height: 180 });
    draw(surface(), [
      [100, 100],
      [200, 150],
    ]);
    fireEvent.click(sheetButton("Done"));

    const d = onChange.mock.calls[0][0] as string;
    expect(d.startsWith(PRIOR + " ")).toBe(true);
    expect(d.length).toBeGreaterThan(PRIOR.length);
  });

  it("passes the value through BYTE-IDENTICALLY — never re-normalised", () => {
    // The 600x180 space is an out-of-band contract with form_pdf.py; a "helpful"
    // normalisation here would bake a permanent stretch into the filed PDF.
    const odd = "M 0.01 179.99 L 599.99 0.01";
    const { container } = render(<SignaturePad value={odd} />);
    expect(
      (container.querySelector('svg[role="img"] path') as SVGPathElement).getAttribute("d"),
    ).toBe(odd);
  });

  it("a controlled parent that does not echo leaves the pad inert — pinned as contract", () => {
    const onChange = vi.fn();
    const { container } = render(<SignaturePad value="" onChange={onChange} />);
    openSheet(container);
    mockRect(surface(), { width: 600, height: 180 });
    draw(surface(), [
      [10, 10],
      [20, 24],
    ]);
    fireEvent.click(sheetButton("Done"));

    expect(onChange).toHaveBeenCalledTimes(1); // the parent WAS told
    // ...but it did not echo, so the preview reflects the prop, not the draft.
    expect(
      (container.querySelector('svg[role="img"] path') as SVGPathElement).getAttribute("d"),
    ).toBe("");
    expect(container.querySelector(".sig__hint")?.textContent).toBe("Tap to sign");
  });

  it("omitting `value` keeps the uncontrolled behaviour every other test relies on", () => {
    const onChange = vi.fn();
    const { container } = render(<SignaturePad onChange={onChange} />);
    openSheet(container);
    mockRect(surface(), { width: 600, height: 180 });
    draw(surface(), [
      [10, 10],
      [20, 24],
    ]);
    fireEvent.click(sheetButton("Done"));

    const committed = onChange.mock.calls[0][0] as string;
    expect(
      (container.querySelector('svg[role="img"] path') as SVGPathElement).getAttribute("d"),
    ).toBe(committed);
  });
});

// ── onDraftDirty (unsaved-work guard) ────────────────────────────────────────

describe("SignaturePad — onDraftDirty", () => {
  it("does NOT fire on sheet open — open-then-Cancel must stay clean", () => {
    const onDraftDirty = vi.fn();
    const { container } = render(<SignaturePad onDraftDirty={onDraftDirty} />);
    openSheet(container);
    expect(onDraftDirty).not.toHaveBeenCalled();
    fireEvent.click(sheetButton("Cancel"));
    expect(onDraftDirty).not.toHaveBeenCalled();
  });

  it("fires on the first COMPLETED stroke — long before Done", () => {
    const onDraftDirty = vi.fn();
    const { container } = render(<SignaturePad onDraftDirty={onDraftDirty} />);
    openSheet(container);
    mockRect(surface(), { width: 600, height: 180 });
    draw(surface(), [
      [10, 10],
      [20, 24],
    ]);
    expect(onDraftDirty).toHaveBeenCalledTimes(1);
    expect(sheet(), "still open — the guard must arm BEFORE commit").not.toBeNull();
  });

  it("fires ONCE for a multi-stroke signature", () => {
    const onDraftDirty = vi.fn();
    const { container } = render(<SignaturePad onDraftDirty={onDraftDirty} />);
    openSheet(container);
    mockRect(surface(), { width: 600, height: 180 });
    for (const stroke of [
      [
        [10, 10],
        [20, 24],
      ],
      [
        [40, 40],
        [50, 60],
      ],
      [
        [80, 20],
        [90, 30],
      ],
    ] as Array<Array<[number, number]>>) {
      draw(surface(), stroke);
    }
    expect(onDraftDirty).toHaveBeenCalledTimes(1);
  });

  it("does NOT fire on a stray tap with no movement", () => {
    const onDraftDirty = vi.fn();
    const { container } = render(<SignaturePad onDraftDirty={onDraftDirty} />);
    openSheet(container);
    mockRect(surface(), { width: 600, height: 180 });
    fireEvent.pointerDown(surface(), { clientX: 10, clientY: 10, pointerId: 1 });
    fireEvent.pointerUp(surface(), { clientX: 10, clientY: 10, pointerId: 1 });
    expect(onDraftDirty).not.toHaveBeenCalled();
  });
});
