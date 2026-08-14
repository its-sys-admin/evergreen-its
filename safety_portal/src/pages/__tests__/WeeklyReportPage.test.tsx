/**
 * Weekly Production Report office screen (0067).
 *
 * The three behaviours the printed document depends on:
 *   1. the narrative textareas are PRE-FILLED with the assembled seed for an unsaved week —
 *      `saved` is row-level, so an untouched narrative renders BLANK once anything is saved;
 *   2. a carried-forward week says so and is not mistaken for a reviewed one;
 *   3. the three-state photo contract survives the round trip — no curation omits `photos`
 *      entirely (auto-select stays in force), "use no photos" sends [].
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/fieldops_report", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/fieldops_report")>();
  return { ...actual, fetchWeeklyReport: vi.fn(), saveWeeklyReport: vi.fn() };
});
// The page renders through PageShell (2026-08 design pass) so it carries the Evergreen header
// and sign-out like every other page. PageShell calls useAuth, which throws outside a provider —
// mocking it is the JobSchedulePage / JobMaterialsPage convention.
vi.mock("../../lib/auth", () => ({ useAuth: vi.fn() }));

import { fetchWeeklyReport, saveWeeklyReport } from "../../lib/fieldops_report";
import type { ProductionReportResponse } from "../../lib/fieldops_report";
import { useAuth } from "../../lib/auth";
import { WeeklyReportPage } from "../WeeklyReportPage";

function payload(over: Partial<ProductionReportResponse> = {}): ProductionReportResponse {
  const base: ProductionReportResponse = {
    job_id: "JOB-1",
    week: { start: "2026-08-08", end: "2026-08-14", from: 1, to: 2 },
    job: { project_name: "Steger Solar", address: "", address_city: "Steger", address_state: "IL",
           job_no: "2026.384", site_phase: 1, status: "active" },
    daily_report_count: 2,
    weather: { days: [
      { work_date: "2026-08-10", conditions: "Clear", avg_temp: "79", inclement: false },
      { work_date: "2026-08-12", conditions: "Heavy rain", avg_temp: "71", inclement: false },
    ], weather_days_week: 0, weather_days_to_date: 7 },
    labor: { total_hours: 122, crews: [{ company: "Pro Panel", workers: 9, days: 3 }], seed_source: "daily" as const },
    crew_progress: [],
    daily_notes: [
      { work_date: "2026-08-10", tomorrows_goals: "Drive rows 19-24", comments: "",
        safety_observations: "", manpower_total: "15", prepared_by: "PM" },
      { work_date: "2026-08-12", tomorrows_goals: "Pump trenches", comments: "Rain day — crew released.",
        safety_observations: "", manpower_total: "3", prepared_by: "PM" },
    ],
    hazard_form_codes: ["jha-v3"],
    deliveries: [],
    material_incidents: [
      { work_date: "2026-08-12", material: "Torque tube", issue: "Short", details: "28 of 40." },
    ],
    photos: {
      available: [
        { pool_id: 1, work_date: "2026-08-10", box_file_id: "b1", caption: "Piles", has_thumb: false },
        { pool_id: 2, work_date: "2026-08-12", box_file_id: "b2", caption: "Trench", has_thumb: false },
      ],
      selected: [{ pool_id: 1, work_date: "2026-08-10", box_file_id: "b1", caption: "Piles" }],
      auto_selected: true,
    },
    schedule: null,
    office: {
      header: { site_location: "", ess_management: "", mobilization_date: "", subcontractors: [], prepared_by: "" },
      safety: {}, weather: { inclement_dates: [], weather_days_to_date: 7 },
      labor: { rows: [] },
      narrative: { critical_items: null, upcoming_activities: null, hazard_topics: [] },
      pending: { rfis: "", submittals: "", ifc_review: "", change_orders: "" },
      photos: null, saved: false, carried_from: null, updated_by: null, updated_at: null,
    },
    generated_at: 1,
  };
  return { ...base, ...over };
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(saveWeeklyReport).mockResolvedValue(undefined);
  vi.mocked(useAuth).mockReturnValue({
    user: { username: "office.admin", role: "admin", capabilities: ["cap.jobtracker.manage"] },
    loading: false,
    login: vi.fn(),
    logout: vi.fn(),
  });
});
afterEach(cleanup);

async function renderPage(p: ProductionReportResponse) {
  vi.mocked(fetchWeeklyReport).mockResolvedValue(p);
  render(<WeeklyReportPage jobId="JOB-1" onBack={() => {}} />);
  await waitFor(() => expect(screen.getByText(/Report header/)).toBeTruthy());
}

describe("WeeklyReportPage — the narrative seed", () => {
  it("pre-fills the narrative from the week's field text on an UNSAVED week", async () => {
    await renderPage(payload());
    const critical = screen.getByLabelText(/Critical items/) as HTMLTextAreaElement;
    const upcoming = screen.getByLabelText(/Upcoming activities/) as HTMLTextAreaElement;
    // Material incident first, then the daily comment — the assembler's order.
    expect(critical.value).toContain("Torque tube: Short — 28 of 40.");
    expect(critical.value).toContain("Rain day — crew released.");
    // The LAST tomorrow's-goals only; earlier days describe work that has since happened.
    expect(upcoming.value).toBe("Pump trenches");
    expect(upcoming.value).not.toContain("Drive rows 19-24");
  });

  it("shows the office's own text, including a deliberately cleared section", async () => {
    const p = payload();
    p.office.saved = true;
    p.office.narrative = { critical_items: "", upcoming_activities: "Module install.", hazard_topics: [] };
    await renderPage(p);
    // Deliberately cleared stays cleared — re-seeding would make clearing impossible.
    expect((screen.getByLabelText(/Critical items/) as HTMLTextAreaElement).value).toBe("");
    expect((screen.getByLabelText(/Upcoming activities/) as HTMLTextAreaElement).value).toBe("Module install.");
  });
});

describe("WeeklyReportPage — the schedule binding", () => {
  const withSchedule = (behind: ProductionReportResponse["schedule"] extends null ? never : NonNullable<ProductionReportResponse["schedule"]>["behind"] = []) => {
    const p = payload();
    p.schedule = {
      sections: [{ name: "Mechanical", items: [{ label: "Piles", percent: 95 }] }],
      behind, today: "2026-08-14", task_count: 1, truncated: false,
    };
    return p;
  };

  it("reports the task count and that nothing is behind", async () => {
    await renderPage(withSchedule());
    expect(screen.getByText(/1 active task\(s\), none behind schedule/)).toBeTruthy();
  });

  it("seeds Critical Items with behind-schedule tasks, ahead of the other sources", async () => {
    await renderPage(withSchedule([
      { name: "Piles", section: "Mechanical", finish_date: "2026-07-01", percent: 40, is_contract_milestone: false },
    ]));
    const critical = (screen.getByLabelText(/Critical items/) as HTMLTextAreaElement).value;
    // This MUST match wpr_data._assemble_critical_items — the screen shows what the report renders.
    expect(critical.split("\n")[0]).toBe("Behind schedule — Mechanical: Piles (due 2026-07-01, 40% complete)");
    expect(critical).toContain("Torque tube: Short");
    expect(screen.getByText(/1 behind schedule as of 2026-08-14/)).toBeTruthy();
  });

  it("labels a slipped contract milestone", async () => {
    await renderPage(withSchedule([
      { name: "Mechanical completion", section: "", finish_date: "2026-06-01", percent: 10, is_contract_milestone: true },
    ]));
    const critical = (screen.getByLabelText(/Critical items/) as HTMLTextAreaElement).value;
    expect(critical.split("\n")[0]).toContain("Contract milestone behind schedule — Mechanical completion");
  });
});

describe("WeeklyReportPage — narrative touched-ness is per field", () => {
  it("seeds an UNTOUCHED field even when the row is saved", async () => {
    const p = payload();
    p.office.saved = true;  // they saved the OSHA counts and never opened the narrative
    p.office.narrative = { critical_items: null, upcoming_activities: null, hazard_topics: [] };
    await renderPage(p);
    expect((screen.getByLabelText(/Critical items/) as HTMLTextAreaElement).value)
      .toContain("Torque tube: Short");
  });

  it("keeps one field's text while seeding the other", async () => {
    const p = payload();
    p.office.saved = true;
    p.office.narrative = { critical_items: "Tracker slipped.", upcoming_activities: null, hazard_topics: [] };
    await renderPage(p);
    expect((screen.getByLabelText(/Critical items/) as HTMLTextAreaElement).value).toBe("Tracker slipped.");
    expect((screen.getByLabelText(/Upcoming activities/) as HTMLTextAreaElement).value).toBe("Pump trenches");
  });
});

describe("WeeklyReportPage — provenance and warnings", () => {
  it("says so when the values were carried forward", async () => {
    const p = payload();
    p.office.carried_from = "2026-08-01";
    await renderPage(p);
    expect(screen.getByText(/carried forward from the week of/)).toBeTruthy();
    expect(screen.getByText(/2026-08-01/)).toBeTruthy();
  });

  it("warns that a week with no dailies will be HELD rather than sent", async () => {
    await renderPage(payload({ daily_report_count: 0 }));
    expect(screen.getByText(/will HOLD this week/)).toBeTruthy();
  });

  it("states the no-schedule case rather than implying zero progress", async () => {
    await renderPage(payload());
    expect(screen.getByText(/No schedule is imported for this job/)).toBeTruthy();
  });

  it("badges the Labor section too on a carried week — carried labor rows outrank the seed and must not look freshly derived", async () => {
    const p = payload();
    p.office.carried_from = "2026-08-01";
    p.office.labor.rows = [{ company: "Stale Sub LLC", workers: "9", man_hours: "540" }];
    await renderPage(p);
    const labor = document.getElementById("wpr-labor");
    expect(labor).toBeTruthy();
    expect(labor!.querySelector(".wpr-carried")?.textContent).toBe("Carried forward");
  });

  it("names the JHA sign-ins as the seed source when the seed is JHA-derived", async () => {
    const p = payload();
    p.labor = { total_hours: 122, crews: [{ company: "Evergreen Renewables", workers: 4, days: 2 }], seed_source: "jha" };
    await renderPage(p);
    expect(screen.getByText(/seeded from this week's JHA sign-ins/)).toBeTruthy();
  });

  it("names the daily reports when the week had no JHA sign-ins", async () => {
    await renderPage(payload());
    expect(screen.getByText(/seeded from the daily reports/)).toBeTruthy();
  });

  it("drops the seed sentence entirely once office labor rows exist — the seed is no longer what displays", async () => {
    const p = payload();
    p.office.labor.rows = [{ company: "Pro Panel", workers: "9", man_hours: "540" }];
    await renderPage(p);
    expect(screen.queryByText(/seeded from/)).toBeNull();
  });
});

describe("WeeklyReportPage — the schedule mesh + truncation (A5)", () => {
  it("links to the schedule page from the page-3 hint when the opener is passed", async () => {
    const onOpenSchedule = vi.fn();
    vi.mocked(fetchWeeklyReport).mockResolvedValue(payload());
    render(<WeeklyReportPage jobId="JOB-1" onBack={() => {}} onOpenSchedule={onOpenSchedule} />);
    await waitFor(() => expect(screen.getByText(/Report header/)).toBeTruthy());
    fireEvent.click(screen.getByText(/Open the schedule page/));
    expect(onOpenSchedule).toHaveBeenCalledWith("JOB-1");
  });

  it("warns when page 3 is a partial table", async () => {
    const p = payload();
    p.schedule = {
      sections: [{ name: "Mechanical", items: [{ label: "Piles", percent: 95 }] }],
      behind: [], today: "2026-08-14", task_count: 600, truncated: true,
    };
    await renderPage(p);
    expect(screen.getByText(/shows the first 600 tasks/)).toBeTruthy();
  });
});

describe("WeeklyReportPage — the three-state photo contract", () => {
  it("OMITS photos from the save when the office has not curated", async () => {
    await renderPage(payload());
    fireEvent.click(screen.getByText(/Save weekly report inputs/));
    await waitFor(() => expect(saveWeeklyReport).toHaveBeenCalled());
    const body = vi.mocked(saveWeeklyReport).mock.calls[0][0];
    // Absent key = auto-select stays in force. Sending [] would mean "no photos this week".
    expect("photos" in body).toBe(false);
  });

  it("sends an EMPTY LIST when the office chooses no photos", async () => {
    await renderPage(payload());
    fireEvent.click(screen.getByText(/Use no photos this week/));
    fireEvent.click(screen.getByText(/Save weekly report inputs/));
    await waitFor(() => expect(saveWeeklyReport).toHaveBeenCalled());
    expect(vi.mocked(saveWeeklyReport).mock.calls[0][0].photos).toEqual([]);
  });

  it("sends the office's explicit picks once they toggle one", async () => {
    await renderPage(payload());
    // Photo 2 starts unselected (server auto-picked only photo 1).
    const boxes = screen.getAllByRole("checkbox");
    const photoBox = boxes[boxes.length - 1];
    fireEvent.click(photoBox);
    fireEvent.click(screen.getByText(/Save weekly report inputs/));
    await waitFor(() => expect(saveWeeklyReport).toHaveBeenCalled());
    const sent = vi.mocked(saveWeeklyReport).mock.calls[0][0].photos;
    expect(sent).toBeDefined();
    expect(sent!.map((p) => p.pool_id).sort()).toEqual([1, 2]);
  });
});

describe("WeeklyReportPage — the office fields D1 cannot derive", () => {
  it("saves the six OSHA counts, labor hours and pending items", async () => {
    await renderPage(payload());
    fireEvent.change(screen.getByLabelText(/Pending RFIs/), { target: { value: "RFI-014" } });
    fireEvent.change(screen.getByLabelText(/Total weather days to date/), { target: { value: "9" } });
    fireEvent.click(screen.getByText(/Save weekly report inputs/));
    await waitFor(() => expect(saveWeeklyReport).toHaveBeenCalled());
    const body = vi.mocked(saveWeeklyReport).mock.calls[0][0];
    expect(body.pending?.rfis).toBe("RFI-014");
    expect(body.weather?.weather_days_to_date).toBe(9);
    // All six OSHA rows are always sent, so a blank grid saves as explicit zeros.
    expect(Object.keys(body.safety ?? {}).sort()).toEqual(
      ["first_aid", "job_transfer", "lost_time", "lost_work_days", "near_miss", "other_recordable"],
    );
    // The labor table seeded from the crews the field reported, hours left for the office.
    expect(body.labor?.rows?.[0]).toMatchObject({ company: "Pro Panel", man_hours: "" });
  });

  it("PRESERVES hazard_topics, which this screen cannot edit and used to wipe on every save", async () => {
    const p = payload();
    p.office.narrative = {
      critical_items: "", upcoming_activities: "", hazard_topics: ["Ladder safety", "Heat illness"],
    };
    await renderPage(p);
    fireEvent.click(screen.getByText(/Save weekly report inputs/));
    await waitFor(() => expect(saveWeeklyReport).toHaveBeenCalled());
    // The page has no editor for this field, so a save from here must round-trip it. It used
    // to send a literal [] — silently clearing a field nothing on screen even displayed.
    expect(vi.mocked(saveWeeklyReport).mock.calls[0][0].narrative?.hazard_topics)
      .toEqual(["Ladder safety", "Heat illness"]);
  });

  it("marks a weather day only when the office ticks it", async () => {
    await renderPage(payload());
    const dayBoxes = screen.getAllByRole("checkbox");
    fireEvent.click(dayBoxes[1]); // 2026-08-12, the heavy-rain day
    fireEvent.click(screen.getByText(/Save weekly report inputs/));
    await waitFor(() => expect(saveWeeklyReport).toHaveBeenCalled());
    expect(vi.mocked(saveWeeklyReport).mock.calls[0][0].weather?.inclement_dates).toEqual(["2026-08-12"]);
  });
});

// The FieldOpsJobTracker rail test's twin (see "a rail chip tap scrolls in place" there).
// A fragment navigation fires popstate, and App's popstate handler REMOUNTS the routed
// page — which on THIS page silently discards an unsaved draft (beforeunload only guards
// real unloads). The fix landed with the job-detail one (PR #119); this pins it locally.
describe("WeeklyReportPage — rail chips never navigate", () => {
  it("a rail chip tap scrolls in place and preserves the draft (the popstate remount trap)", async () => {
    // jsdom has no scrollIntoView; install one so the handler's scroll call is observable.
    const scrollSpy = vi.fn();
    const orig = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = scrollSpy;
    try {
      await renderPage(payload());
      // Dirty the draft first — the state a remount would silently destroy.
      const critical = screen.getByLabelText(/Critical items/) as HTMLTextAreaElement;
      fireEvent.change(critical, { target: { value: "edited but unsaved" } });
      const fetches = vi.mocked(fetchWeeklyReport).mock.calls.length;
      const chip = document.querySelector('.wpr__rail-link[href="#wpr-labor"]')!;
      // fireEvent.click returns FALSE when the handler preventDefault()ed — the whole point.
      expect(fireEvent.click(chip)).toBe(false);
      expect(scrollSpy).toHaveBeenCalled();
      // No remount: no refetch, and the unsaved edit is still on screen.
      expect(vi.mocked(fetchWeeklyReport).mock.calls.length).toBe(fetches);
      expect((screen.getByLabelText(/Critical items/) as HTMLTextAreaElement).value).toBe("edited but unsaved");
    } finally {
      Element.prototype.scrollIntoView = orig;
    }
  });
});
