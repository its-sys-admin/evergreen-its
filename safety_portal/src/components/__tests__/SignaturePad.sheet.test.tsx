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

  it("refuses a zero-sized rect rather than emitting Infinity into the path", () => {
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
