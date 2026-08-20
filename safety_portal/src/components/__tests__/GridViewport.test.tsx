/**
 * GridViewport — the sticky-header, operator-resizable grid scroll box.
 *
 * The contract under test:
 *   1. children render inside .gridvp__scroll at the compact default (448px, the old
 *      inline 28rem) with maxHeight — NOT height — and nothing persists until the
 *      operator actually interacts;
 *   2. presets apply their px (or the max CSS class, with NO inline style) and persist
 *      under `its-portal-gridvp:v1:<key>`;
 *   3. a drag lands on a custom px via the jsdom zero-rect fallback (start from the
 *      current numeric height) and persists ON RELEASE, never per move;
 *   4. the handle is a keyboard-operable separator (arrows/page/Home/End);
 *   5. junk or throwing storage NEVER breaks the grid — persistence is a convenience,
 *      the default is the fallback.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GridViewport } from "../GridViewport";

const KEY = "its-portal-gridvp:v1:test-grid";

// The SPA jsdom env doesn't reliably provide localStorage (Node's experimental global
// shadows jsdom's), so install a deterministic in-memory Storage — the draftCache.test.ts
// pattern. Production uses the real browser localStorage; GridViewport try/catch'es every
// access for the unavailable case.
function memoryStorage(): Storage {
  const m = new Map<string, string>();
  return {
    get length() {
      return m.size;
    },
    clear: () => m.clear(),
    getItem: (k: string) => (m.has(k) ? m.get(k)! : null),
    key: (i: number) => Array.from(m.keys())[i] ?? null,
    removeItem: (k: string) => void m.delete(k),
    setItem: (k: string, v: string) => void m.set(k, String(v)),
  };
}

function mount(minPx?: number) {
  const r = render(
    <GridViewport storageKey="test-grid" label="Test rows" minPx={minPx}>
      <table className="dash-table">
        <thead>
          <tr>
            <th>Col</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>cell-1</td>
          </tr>
        </tbody>
      </table>
    </GridViewport>,
  );
  const scroll = r.container.querySelector(".gridvp__scroll") as HTMLDivElement;
  const handle = screen.getByRole("separator") as HTMLDivElement;
  return { ...r, scroll, handle };
}

beforeEach(() => {
  vi.stubGlobal("localStorage", memoryStorage());
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("GridViewport", () => {
  it("renders children in the scroll box at the 448px compact default without touching storage", () => {
    const { scroll, container } = mount();
    expect(scroll.textContent).toContain("cell-1");
    expect(scroll.style.maxHeight).toBe("448px");
    // maxHeight, not height — a short table must stay content-height.
    expect(scroll.style.height).toBe("");
    expect(localStorage.getItem(KEY)).toBeNull();
    expect(localStorage.length).toBe(0);
    const compact = container.querySelector('[aria-pressed="true"]') as HTMLButtonElement;
    expect(compact.textContent).toBe("Compact");
  });

  it("Tall preset: 704px, persisted as 'tall', and the pressed state moves", () => {
    const { scroll } = mount();
    fireEvent.click(screen.getByRole("button", { name: "Tall" }));
    expect(scroll.style.maxHeight).toBe("704px");
    expect(localStorage.getItem(KEY)).toBe("tall");
    expect(screen.getByRole("button", { name: "Tall" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: "Compact" }).getAttribute("aria-pressed")).toBe("false");
    expect(screen.getByRole("button", { name: "Max" }).getAttribute("aria-pressed")).toBe("false");
  });

  it("Max preset: the CSS modifier carries the height — no inline style — and 'max' persists", () => {
    const { scroll } = mount();
    fireEvent.click(screen.getByRole("button", { name: "Max" }));
    expect(scroll.className).toContain("gridvp__scroll--max");
    expect(scroll.style.maxHeight).toBe("");
    expect(localStorage.getItem(KEY)).toBe("max");
  });

  it("drag: custom px from the current height (jsdom zero-rect fallback), saved on release only", () => {
    const { scroll, handle } = mount();
    fireEvent.pointerDown(handle, { clientY: 100, pointerId: 1 });
    fireEvent.pointerMove(handle, { clientY: 180, pointerId: 1 });
    // Moves apply live but never persist — localStorage writes would jank the drag.
    expect(scroll.style.maxHeight).toBe("528px"); // 448 (compact fallback) + 80
    expect(localStorage.getItem(KEY)).toBeNull();
    fireEvent.pointerUp(handle, { pointerId: 1 });
    expect(scroll.style.maxHeight).toBe("528px");
    expect(localStorage.getItem(KEY)).toBe("528");
    // Any drag → custom: no preset claims to be current.
    for (const name of ["Compact", "Tall", "Max"]) {
      expect(screen.getByRole("button", { name }).getAttribute("aria-pressed")).toBe("false");
    }
  });

  it("keyboard: arrows step 16px (Down = taller), Home floors at minPx, End is the max preset", () => {
    const { scroll, handle } = mount();
    handle.focus();
    fireEvent.keyDown(handle, { key: "ArrowDown" });
    fireEvent.keyDown(handle, { key: "ArrowDown" });
    expect(scroll.style.maxHeight).toBe("480px");
    expect(localStorage.getItem(KEY)).toBe("480");
    fireEvent.keyDown(handle, { key: "Home" });
    expect(scroll.style.maxHeight).toBe("160px"); // the default minPx
    expect(localStorage.getItem(KEY)).toBe("160");
    fireEvent.keyDown(handle, { key: "End" });
    expect(scroll.className).toContain("gridvp__scroll--max");
    expect(localStorage.getItem(KEY)).toBe("max");
  });

  it("junk persisted values fall back to the compact default and never throw", () => {
    for (const junk of ["garbage", "999999", "-5"]) {
      localStorage.setItem(KEY, junk);
      const { scroll } = mount();
      expect(scroll.style.maxHeight, `stored ${JSON.stringify(junk)}`).toBe("448px");
      cleanup();
      localStorage.clear();
    }
  });

  it("a throwing localStorage.setItem still lets the height change (persistence is best-effort)", () => {
    const throwing = memoryStorage();
    throwing.setItem = () => {
      throw new Error("quota");
    };
    vi.stubGlobal("localStorage", throwing);
    const { scroll } = mount();
    fireEvent.click(screen.getByRole("button", { name: "Tall" }));
    expect(scroll.style.maxHeight).toBe("704px");
  });

  it("PERF CONTRACT: drag frames never re-render the children subtree", () => {
    // Height state lives inside GridViewport and `children` arrives as a stable element
    // reference, so React's identity bailout must skip the (potentially 200-col ×
    // 1000+-row) table subtree on every drag frame. A clone/augment of children — or
    // hoisting height state up — would break exactly this assertion.
    const renders = { count: 0 };
    function Spy() {
      renders.count += 1;
      return <p>spy-child</p>;
    }
    render(
      <GridViewport storageKey="test-grid" label="Test rows">
        <Spy />
      </GridViewport>,
    );
    expect(renders.count).toBe(1);
    const handle = screen.getByRole("separator") as HTMLDivElement;
    fireEvent.pointerDown(handle, { clientY: 100, pointerId: 1 });
    fireEvent.pointerMove(handle, { clientY: 140, pointerId: 1 });
    fireEvent.pointerMove(handle, { clientY: 180, pointerId: 1 });
    fireEvent.pointerUp(handle, { pointerId: 1 });
    fireEvent.click(screen.getByRole("button", { name: "Tall" }));
    expect(
      renders.count,
      "GridViewport's own height updates must bail out of the children subtree",
    ).toBe(1);
  });

  it("a11y: the handle is a labelled horizontal separator with a value, the presets a named group", () => {
    const { handle, container } = mount();
    expect(handle.getAttribute("role")).toBe("separator");
    expect(handle.getAttribute("aria-orientation")).toBe("horizontal");
    expect(handle.getAttribute("aria-label")).toBe("Resize Test rows");
    expect(handle.getAttribute("aria-valuemin")).toBe("160");
    expect(handle.getAttribute("aria-valuemax")).toBe("1600");
    expect(handle.getAttribute("aria-valuenow")).toBe("448");
    expect(handle.tabIndex).toBe(0);
    const group = container.querySelector('[role="group"]') as HTMLDivElement;
    expect(group.getAttribute("aria-label")).toBe("Test rows height");
  });
});
