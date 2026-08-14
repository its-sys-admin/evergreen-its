/**
 * SectionRail — the shared two-column rail. The ONE rule it exists to keep: chips scroll
 * directly and never touch history (a fragment nav fires popstate → App remounts the page,
 * discarding state/drafts — the defect class both pages fixed independently before extraction).
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SectionRail } from "../SectionRail";

afterEach(cleanup);

const SECTIONS = [
  { id: "s-one", label: "One" },
  { id: "s-two", label: "Two" },
];

describe("SectionRail", () => {
  it("renders chips with the page's class family and lights the active one", () => {
    render(<SectionRail sections={SECTIONS} activeId="s-two" ariaLabel="Test sections" classPrefix="job-rail" />);
    const nav = screen.getByLabelText("Test sections");
    expect(nav.className).toBe("job-rail");
    const links = nav.querySelectorAll("a.job-rail__link");
    expect(links).toHaveLength(2);
    expect(links[1].getAttribute("aria-current")).toBe("true");
    expect(links[0].getAttribute("aria-current")).toBeNull();
  });

  it("scrolls directly on click and never navigates — no popstate remount", () => {
    const target = document.createElement("div");
    target.id = "s-one";
    target.scrollIntoView = vi.fn();
    document.body.appendChild(target);
    const before = window.location.href;
    render(<SectionRail sections={SECTIONS} activeId={null} ariaLabel="Test sections" classPrefix="wpr__rail" />);
    fireEvent.click(screen.getByText("One"));
    expect(target.scrollIntoView).toHaveBeenCalledOnce();
    expect(window.location.href).toBe(before); // the href fragment never landed in history
    target.remove();
  });
});
