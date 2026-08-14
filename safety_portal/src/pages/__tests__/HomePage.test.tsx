/**
 * HomePage — headed sections (Daily forms / Field operations / Office operations / Administration)
 * replacing the flat card wall. Section membership is presentation ONLY: every card keeps its exact
 * capability gate and view key (proven per-card below), an empty section renders no heading, and
 * the named copy edits landed (My Tasks mentions the daily checklist; the admin card is
 * "Checklists", renamed from "Inspection checklists"). The office-facing management cards (POs,
 * subcontracts, catalogs) live under "Office operations" (2026-07); Administration keeps the
 * config/identity + account cards.
 */
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/auth", () => ({ useAuth: vi.fn() }));

import { HomePage } from "../HomePage";
import { useAuth } from "../../lib/auth";

function authWith(capabilities: string[]) {
  return {
    user: { username: "sam", role: "submitter" as const, capabilities },
    loading: false,
    login: vi.fn(async () => {}),
    logout: vi.fn(async () => {}),
  };
}

afterEach(cleanup);
beforeEach(() => {
  vi.resetAllMocks();
});

const ALL_CAPS = [
  "cap.form.submit",
  "cap.form.request",
  "cap.tasks.own",
  "cap.jobtracker.read",
  "cap.equipment.field",
  "cap.personnel.read",
  "cap.materials.manage",
  "cap.checklist.manage",
  "cap.po.manage",
  "cap.subcontracts.manage",
  "cap.admin.accounts",
  "cap.admin.formbuilder",
];

// Every card: its title, gating cap, view key, and section heading. Gating must be UNCHANGED by
// the regrouping — this table IS the regression net. The six management cards moved to "Office
// operations" (2026-07); Administration keeps PO/SC Configuration, Forms, Accounts.
const CARDS: { title: string; cap: string; nav: string; section: string }[] = [
  { title: "Submit a form", cap: "cap.form.submit", nav: "fill", section: "Daily forms" },
  { title: "Form Request", cap: "cap.form.request", nav: "request", section: "Daily forms" },
  { title: "My Tasks", cap: "cap.tasks.own", nav: "fieldops-tasks", section: "Field operations" },
  { title: "Job Tracker", cap: "cap.jobtracker.read", nav: "fieldops-jobs", section: "Field operations" },
  { title: "Equipment", cap: "cap.equipment.field", nav: "fieldops-equipment", section: "Field operations" },
  { title: "Personnel", cap: "cap.personnel.read", nav: "fieldops-personnel", section: "Field operations" },
  // 2026-07 fold: RFQs + Vendor Estimates are TABS inside the Purchase Orders hub — one card.
  { title: "Purchase Orders", cap: "cap.po.manage", nav: "po-builder", section: "Office operations" },
  { title: "Subcontracts", cap: "cap.subcontracts.manage", nav: "subcontract-builder", section: "Office operations" },
  { title: "Checklists", cap: "cap.checklist.manage", nav: "fieldops-inspections", section: "Office operations" },
  { title: "Materials Catalog", cap: "cap.materials.manage", nav: "materials-catalog", section: "Office operations" },
  { title: "Vendors", cap: "cap.po.manage", nav: "po-vendors", section: "Office operations" },
  { title: "Subcontractors", cap: "cap.subcontracts.manage", nav: "subcontractors", section: "Office operations" },
  { title: "PO/SC Configuration", cap: "cap.po.manage", nav: "po-config", section: "Administration" },
  { title: "Forms", cap: "cap.admin.formbuilder", nav: "forms", section: "Administration" },
  { title: "Accounts", cap: "cap.admin.accounts", nav: "accounts", section: "Administration" },
];

function cardTitles(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll(".form-card__title")).map((el) => el.textContent ?? "");
}
function sectionHeadings(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll(".page__heading")).map((el) => el.textContent ?? "");
}

describe("HomePage — R7 sections", () => {
  it("an all-caps admin sees the four headed sections with the cards grouped under them", () => {
    vi.mocked(useAuth).mockReturnValue(authWith(ALL_CAPS));
    const { container } = render(<HomePage onNavigate={() => {}} />);
    expect(sectionHeadings(container)).toEqual([
      "Daily forms",
      "Field operations",
      "Office operations",
      "Administration",
    ]);
    for (const c of CARDS) {
      const section = container.querySelector(`section[aria-label="${c.section}"]`)!;
      const titles = Array.from(section.querySelectorAll(".form-card__title")).map((el) => el.textContent);
      expect(titles, `${c.title} in ${c.section}`).toContain(c.title);
    }
  });

  it("Office operations holds the six management cards in the operator's order; Administration keeps three", () => {
    vi.mocked(useAuth).mockReturnValue(authWith(ALL_CAPS));
    const { container } = render(<HomePage onNavigate={() => {}} />);
    const titlesIn = (heading: string) =>
      Array.from(
        container.querySelector(`section[aria-label="${heading}"]`)!.querySelectorAll(".form-card__title"),
      ).map((el) => el.textContent);
    // Array order in HOME_CARDS IS the two-wide display order — pin the exact sequence the
    // operator asked for. 2026-07 fold: the Vendor Estimates + RFQs cards are gone — both
    // live as tabs inside the Purchase Orders hub now (the card copy below names them).
    expect(titlesIn("Office operations")).toEqual([
      "Purchase Orders",
      "Subcontracts",
      "Checklists",
      "Materials Catalog",
      "Vendors",
      "Subcontractors",
    ]);
    expect(titlesIn("Administration")).toEqual(["PO/SC Configuration", "Forms", "Accounts"]);
  });

  it("the folded lanes stay discoverable: the PO card copy names RFQs and vendor estimates", () => {
    vi.mocked(useAuth).mockReturnValue(authWith(ALL_CAPS));
    const { container } = render(<HomePage onNavigate={() => {}} />);
    const poCard = Array.from(container.querySelectorAll(".form-card")).find(
      (el) => el.querySelector(".form-card__title")?.textContent === "Purchase Orders",
    )!;
    expect(poCard.textContent ?? "").toContain("RFQs");
    expect(poCard.textContent ?? "").toContain("vendor estimates");
    expect(cardTitles(container)).not.toContain("Vendor Estimates");
    expect(cardTitles(container)).not.toContain("RFQs");
  });

  it("a submitter (no admin caps) gets NO Office/Administration heading — empty sections render nothing", () => {
    vi.mocked(useAuth).mockReturnValue(
      authWith(["cap.form.submit", "cap.form.request", "cap.tasks.own", "cap.jobtracker.read"]),
    );
    const { container } = render(<HomePage onNavigate={() => {}} />);
    expect(sectionHeadings(container)).toEqual(["Daily forms", "Field operations"]);
    expect(container.textContent ?? "").not.toContain("Office operations");
    expect(container.textContent ?? "").not.toContain("Administration");
    expect(cardTitles(container)).toEqual(["Submit a form", "Form Request", "My Tasks", "Site Tasks", "Job Tracker"]);
  });

  it("every card keeps its exact capability gate (present with the cap, absent without)", () => {
    for (const c of CARDS) {
      vi.mocked(useAuth).mockReturnValue(authWith([c.cap]));
      const withCap = render(<HomePage onNavigate={() => {}} />);
      expect(cardTitles(withCap.container), `${c.title} with ${c.cap}`).toContain(c.title);
      withCap.unmount();

      const others = ALL_CAPS.filter((k) => k !== c.cap);
      vi.mocked(useAuth).mockReturnValue(authWith(others));
      const withoutCap = render(<HomePage onNavigate={() => {}} />);
      expect(cardTitles(withoutCap.container), `${c.title} without ${c.cap}`).not.toContain(c.title);
      withoutCap.unmount();
    }
  });

  it("every card keeps its view key: clicking navigates to the unchanged HomeNav target", () => {
    vi.mocked(useAuth).mockReturnValue(authWith(ALL_CAPS));
    const onNavigate = vi.fn();
    const { container } = render(<HomePage onNavigate={onNavigate} />);
    for (const c of CARDS) {
      const card = Array.from(container.querySelectorAll(".form-card")).find(
        (el) => el.querySelector(".form-card__title")?.textContent === c.title,
      )!;
      fireEvent.click(card);
      expect(onNavigate, c.title).toHaveBeenLastCalledWith(c.nav);
    }
  });

  it("named copy edits: My Tasks names the Daily report (D2); the admin card is 'Checklists' (renamed)", () => {
    vi.mocked(useAuth).mockReturnValue(authWith(ALL_CAPS));
    const { container } = render(<HomePage onNavigate={() => {}} />);
    const myTasks = Array.from(container.querySelectorAll(".form-card")).find(
      (el) => el.querySelector(".form-card__title")?.textContent === "My Tasks",
    )!;
    // D2: the tab is the SOP daily FORM now — the card copy names the Daily report, not the
    // retired checkbox checklist.
    expect(myTasks.textContent ?? "").toContain("Daily report");
    expect(myTasks.textContent ?? "").not.toContain("daily checklist");
    expect(cardTitles(container)).toContain("Checklists");
    expect(cardTitles(container)).not.toContain("Inspection checklists");
  });

  it("badge taxonomy unchanged: Admin badges stay on the management cards, Field Ops on the field cards", () => {
    vi.mocked(useAuth).mockReturnValue(authWith(ALL_CAPS));
    const { container } = render(<HomePage onNavigate={() => {}} />);
    const badgeFor = (title: string) =>
      Array.from(container.querySelectorAll(".form-card"))
        .find((el) => el.querySelector(".form-card__title")?.textContent === title)
        ?.querySelector(".form-card__badge")?.textContent;
    expect(badgeFor("My Tasks")).toBe("Field Ops");
    expect(badgeFor("Job Tracker")).toBe("Field Ops");
    expect(badgeFor("Checklists")).toBe("Admin");
    expect(badgeFor("Accounts")).toBe("Admin");
    expect(badgeFor("Personnel")).toBe("Admin");
  });
});
