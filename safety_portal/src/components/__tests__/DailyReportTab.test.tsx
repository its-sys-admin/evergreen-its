/**
 * DailyReportTab (SOP daily form, slice D2) — section-level detail tests: the Daily tab IS the
 * daily-report-v2 form now. Covers: date selector (Pacific today default, max today) + job line +
 * the v2 SOP form rendered inline through the REAL definition/renderer; the R2-carried explanatory
 * empty states (not-manager / unlinked / unplaced); never-silent placement load (error + working
 * Retry); best-effort crew/equipment/prepared_by prefill (+ the soft warn on failure); form_link
 * deep-links via the R3 openForm machinery + the live "Filed ✓" indicators; the "already filed"
 * banner + load-&-amend; the standard submit path (idempotent submission id, amends_uuid);
 * past-date filed-state-first collapse. Page-level integration (tabs, auto-switch, refresh) lives
 * in pages/__tests__/FieldOpsMyTasks.test.tsx.
 */
import { act, cleanup, fireEvent, render, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/auth", () => ({ useAuth: vi.fn() }));
vi.mock("../../lib/fieldops_jobtracker", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/fieldops_jobtracker")>();
  return { ...actual, fetchJobList: vi.fn(), fetchJobDetail: vi.fn() };
});
vi.mock("../../lib/fieldops_daily_form", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/fieldops_daily_form")>();
  return { ...actual, fetchDailyFormStatus: vi.fn(), fetchDailyRequirements: vi.fn() };
});
vi.mock("../../lib/fieldops_expected_materials", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/fieldops_expected_materials")>();
  return {
    ...actual,
    fetchExpectedMaterials: vi.fn(),
    receiveExpectedMaterial: vi.fn(),
    flagExpectedMaterialIncident: vi.fn(),
  };
});
vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return { ...actual, fetchRecent: vi.fn(), submitForm: vi.fn() };
});
vi.mock("../../lib/fieldops_daily_photos", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/fieldops_daily_photos")>();
  // The v6 additional_photos mount fires listDailyPhotos whenever refs exist (the amend flow
  // below); jsdom has no same-origin fetch, so the pool client is a fixture source here.
  return { ...actual, uploadDailyPhoto: vi.fn(), deleteDailyPhoto: vi.fn(), listDailyPhotos: vi.fn() };
});

import * as api from "../../lib/api";
import * as jobs from "../../lib/fieldops_jobtracker";
import type { ViewerTaskPlacement } from "../../lib/fieldops_tasks";
import { fetchDailyFormStatus, fetchDailyRequirements } from "../../lib/fieldops_daily_form";
import type { DailyRequirementItem } from "../../lib/fieldops_daily_form";
import {
  fetchExpectedMaterials,
  flagExpectedMaterialIncident,
  receiveExpectedMaterial,
} from "../../lib/fieldops_expected_materials";
import type { ExpectedMaterialRow } from "../../lib/fieldops_expected_materials";
import { ApiError } from "../../lib/errorCopy";
import { pacificToday } from "../myTasksShared";
import { formCatalog } from "../../forms/registry";
import { DailyReportTab } from "../DailyReportTab";
import { useAuth } from "../../lib/auth";

function authAs(role: "submitter" | "manager" | "admin", capabilities: string[] = ["cap.tasks.own", "cap.jobtracker.read"]) {
  return {
    user: { username: "mgr.mo", role, capabilities },
    loading: false,
    login: vi.fn(async () => {}),
    logout: vi.fn(async () => {}),
  };
}

const TODAY = pacificToday();

// CS4 #12: placement is a PROP now (the parent's /tasks/mine viewer_placement) — the tab no longer
// fetches a Job Tracker list page. The fixture is the exact wire shape the parent forwards.
const PLACED: ViewerTaskPlacement = { job_id: "JOB-A", project_name: "Alpha", personnel_id: 1, name: "Mo Manager" };

const DETAIL: jobs.JobDetailResponse = {
  job: {
    job_id: "JOB-A",
    project_name: "Alpha",
    status: "active",
    lifecycle: "active",
    // Track 6: `archive` is a REQUIRED field on JobDetail, so every mock must carry it.
    // null = this job has never entered the archive workflow.
    archive: null,
    progress: 0,
    job_no: "",
    routing: {
      address: "", address_city: "", address_state: "", address_zip: "",
      stakeholder_name: "", stakeholder_email: "", stakeholder_phone: "",
      safety_contact_name: "", safety_contact_email: "", safety_cc: [],
      progress_contact_name: "", progress_contact_email: "", progress_cc: [],
    },
    client: null,
    crew: [
      { id: 1, name: "Sam", trade: "electrician", account_role: null },
      { id: 2, name: "Lee", trade: null, account_role: null },
    ],
    tasks: [],
    time_entries: [],
    equipment_on_site: [{ id: 9, name: "Excavator", kind: null, identifier: "EX-1", label: null, read_at: null }],
    inspections: [],
  },
  cursors: { tasks: null, time: null, insp: null },
  viewer_personnel: { id: 1, name: "Mo Manager" },
};

const EMPTY_STATUS = { filed: {}, daily_filed: null };

function inputValues(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll("input, textarea")).map((el) => (el as HTMLInputElement).value);
}
function dateInput(container: HTMLElement): HTMLInputElement {
  return container.querySelector('input[type="date"]') as HTMLInputElement;
}

afterEach(cleanup);
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(useAuth).mockReturnValue(authAs("manager"));
  vi.mocked(jobs.fetchJobDetail).mockResolvedValue(DETAIL);
  vi.mocked(fetchDailyFormStatus).mockResolvedValue(EMPTY_STATUS);
  vi.mocked(fetchDailyRequirements).mockResolvedValue([]);
  vi.mocked(fetchExpectedMaterials).mockResolvedValue({ expected_materials: [], shipments: [], receipt_events: [], project_name: "Deep Lake" });
  vi.mocked(receiveExpectedMaterial).mockResolvedValue(undefined);
  vi.mocked(flagExpectedMaterialIncident).mockResolvedValue(undefined);
  vi.mocked(api.fetchRecent).mockResolvedValue(null);
  vi.mocked(api.submitForm).mockResolvedValue(undefined);
});

describe("DailyReportTab — the placed manager's inline SOP form", () => {
  it("renders the date selector (Pacific today, max today), the job line, and the v2 form inline", async () => {
    const { container, getByLabelText } = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(container.textContent ?? "").toContain("Alpha"));
    const dt = dateInput(container);
    expect(dt.value).toBe(TODAY);
    expect(dt.max).toBe(TODAY);
    expect(container.textContent ?? "").toContain("JOB-A");
    // The REAL daily-report-v2 definition rendered through the REAL FormRenderer: SOP guidance,
    // form_link buttons, and the submit control are all present INLINE (no picker anywhere).
    expect(container.textContent ?? "").toContain("SITE SUPERVISOR");
    expect(container.textContent ?? "").toContain("Create Job Hazard Analysis");
    // D3 (daily-report-v3): the manager's photo-upload place renders inline (the
    // site_photos header photo field → PhotoField), and the 50-photo minimum is gone.
    expect(container.textContent ?? "").toContain("Site photos");
    expect(container.querySelector(".photo-field")).not.toBeNull();
    expect(container.textContent ?? "").not.toContain("Minimum 50");
    expect(getByLabelText("Submit daily report")).not.toBeNull();
    expect(container.querySelector("select")).toBeNull(); // no job/form pickers — envelope is fixed
  });

  it("reports the placement up via onLoaded (drives the parent auto-switch + quick actions)", async () => {
    const onLoaded = vi.fn();
    render(<DailyReportTab linked={true} placement={PLACED} onLoaded={onLoaded} />);
    await waitFor(() =>
      expect(onLoaded).toHaveBeenCalledWith({ placement: { job_id: "JOB-A", project_name: "Alpha" } }),
    );
  });

  it("a placed ADMIN renders the form and reports the placement exactly like a manager (directive 2026-07-03)", async () => {
    vi.mocked(useAuth).mockReturnValue(authAs("admin"));
    const onLoaded = vi.fn();
    const { container, getByLabelText } = render(
      <DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} onLoaded={onLoaded} />,
    );
    await waitFor(() => expect(container.textContent ?? "").toContain("SITE SUPERVISOR"));
    expect(getByLabelText("Submit daily report")).not.toBeNull();
    expect(container.textContent ?? "").not.toContain("crew-lead managers");
    await waitFor(() =>
      expect(onLoaded).toHaveBeenCalledWith({ placement: { job_id: "JOB-A", project_name: "Alpha" } }),
    );
  });

  it("prefills crew_progress + equipment_on_site rows and prepared_by from the job detail", async () => {
    const { container } = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(inputValues(container)).toContain("Sam (electrician)"));
    const values = inputValues(container);
    expect(values).toContain("Lee"); // no trade → bare name
    expect(values).toContain("Excavator (EX-1)");
    expect(values).toContain("Mo Manager"); // prepared_by from viewer_personnel
  });

  it("a prefill failure is a soft warn, never a blocker: the form still renders and submits", async () => {
    vi.mocked(jobs.fetchJobDetail).mockRejectedValue(new ApiError(null, 500));
    const { container, getByLabelText } = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(container.textContent ?? "").toContain("Couldn't prefill crew and equipment"));
    expect(getByLabelText("Submit daily report")).not.toBeNull();
    expect((getByLabelText("Submit daily report") as HTMLButtonElement).disabled).toBe(false);
    expect(inputValues(container)).not.toContain("Sam (electrician)");
  });
});

describe("DailyReportTab — R2-carried explanatory empty states (Mandatory A)", () => {
  it("a non-manager gets the crew-lead copy even when a placement rides down (and never a jobs fetch)", async () => {
    vi.mocked(useAuth).mockReturnValue(authAs("submitter"));
    const onLoaded = vi.fn();
    // A placed SUBMITTER's viewer_placement is non-null on the wire — the tab must still gate on
    // the manager role (the parent auto-switch depends on the null report).
    const { container } = render(<DailyReportTab linked={true} placement={PLACED} onLoaded={onLoaded} />);
    await waitFor(() => expect(onLoaded).toHaveBeenCalledWith({ placement: null }));
    expect(container.textContent ?? "").toContain("crew-lead managers");
    expect(jobs.fetchJobList).not.toHaveBeenCalled(); // CS4 #12: the jobs-list stage is deleted
    expect(container.querySelector('input[type="date"]')).toBeNull();
  });

  it("an UNLINKED manager (linked:false) gets the roster-link copy", async () => {
    const { container } = render(<DailyReportTab linked={false} placement={null} />);
    await waitFor(() => expect(container.textContent ?? "").toContain("isn't linked to a roster person"));
    expect(container.textContent ?? "").not.toContain("not placed on a job yet");
  });

  it("a linked-but-unplaced manager gets the not-placed copy (+ onLoaded null)", async () => {
    const onLoaded = vi.fn();
    const { container } = render(<DailyReportTab linked={true} placement={null} onLoaded={onLoaded} />);
    await waitFor(() => expect(container.textContent ?? "").toContain("not placed on a job yet"));
    expect(onLoaded).toHaveBeenCalledWith({ placement: null });
    expect(jobs.fetchJobList).not.toHaveBeenCalled(); // CS4 #12: no fallback jobs fetch either
  });

  it("a parent placement-fetch failure shows the error + a WORKING Retry (never a lying empty)", async () => {
    // CS4 #12: the parent's /tasks/mine read failed with nothing landed (linked null) — the tab
    // shows the error with a Retry wired to the PARENT's load; a rerender with the landed
    // placement (the retried fetch) swaps to the form.
    const onRetryPlacement = vi.fn();
    const view = render(
      <DailyReportTab linked={null} placement={null} placementError="Something went wrong on the server." onRetryPlacement={onRetryPlacement} />,
    );
    await waitFor(() => expect(view.container.textContent ?? "").toContain("Something went wrong on the server"));
    expect(view.container.textContent ?? "").not.toContain("not placed on a job yet");
    fireEvent.click(view.getByLabelText("Retry loading your daily report"));
    expect(onRetryPlacement).toHaveBeenCalledTimes(1);
    view.rerender(<DailyReportTab linked={true} placement={PLACED} />);
    await waitFor(() => expect(view.container.textContent ?? "").toContain("Alpha"));
  });

  it("while the parent fetch is in flight (linked null, no error) the tab shows the loading state", () => {
    const { container } = render(<DailyReportTab linked={null} placement={null} />);
    expect(container.textContent ?? "").toContain("Loading your daily report…");
  });

  it("once landed, a later refresh error never masks the explanatory copy (landed data wins)", async () => {
    const { container } = render(
      <DailyReportTab linked={true} placement={null} placementError="refresh failed" />,
    );
    await waitFor(() => expect(container.textContent ?? "").toContain("not placed on a job yet"));
  });

  it("a null-name placement (soft-ref edge) backfills the project name from the job detail", async () => {
    const onLoaded = vi.fn();
    const { container } = render(
      <DailyReportTab linked={true} placement={{ ...PLACED, project_name: null }} onLoaded={onLoaded} />,
    );
    await waitFor(() => expect(container.textContent ?? "").toContain("Alpha")); // detail-read fill
    await waitFor(() =>
      expect(onLoaded).toHaveBeenCalledWith({ placement: { job_id: "JOB-A", project_name: "Alpha" } }),
    );
  });
});

describe("DailyReportTab — form_link deep-links + filed indicators", () => {
  it("a form_link button deep-links via openForm with the tab's job + date (returnTo rides App)", async () => {
    const onOpenForm = vi.fn();
    const { getByRole } = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={onOpenForm} />);
    const btn = await waitFor(() => getByRole("button", { name: /Create Job Hazard Analysis/ }));
    fireEvent.click(btn);
    expect(onOpenForm).toHaveBeenCalledWith(
      expect.objectContaining({ jobId: "JOB-A", parentCode: "jha", workDate: TODAY }),
    );
  });

  it("renders a live 'Filed ✓ <time> by <name>' indicator from the status endpoint", async () => {
    vi.mocked(fetchDailyFormStatus).mockResolvedValue({
      filed: { jha: { filed_at: 1_700_000_000, filed_by_name: "Sam Submitter" } },
      daily_filed: null,
    });
    const { container } = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(container.textContent ?? "").toContain("Filed ✓"));
    expect(container.textContent ?? "").toContain("by Sam Submitter");
  });

  it("a NULL filed_by_name drops the 'by …' clause (display-name-only, never a raw account)", async () => {
    vi.mocked(fetchDailyFormStatus).mockResolvedValue({
      filed: { jha: { filed_at: 1_700_000_000, filed_by_name: null } },
      daily_filed: null,
    });
    const { container } = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(container.textContent ?? "").toContain("Filed ✓"));
    expect(container.querySelector(".fr__form-link-filed")?.textContent ?? "").not.toContain(" by ");
  });

  it("a status read failure soft-warns with Retry — the form stays fillable (never silent)", async () => {
    vi.mocked(fetchDailyFormStatus)
      .mockRejectedValueOnce(new ApiError(null, 500))
      .mockResolvedValue({ filed: { jha: { filed_at: 1_700_000_000, filed_by_name: null } }, daily_filed: null });
    const { container, getByLabelText } = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    // R1 convention: the ApiError's HUMAN copy surfaces (errMsg falls back only on non-Errors);
    // the `what`-scoped Retry disambiguates it from any sibling error.
    await waitFor(() => expect(getByLabelText("Retry checking filed forms")).not.toBeNull());
    expect(container.textContent ?? "").toContain("Something went wrong on the server");
    expect((getByLabelText("Submit daily report") as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(getByLabelText("Retry checking filed forms"));
    await waitFor(() => expect(container.textContent ?? "").toContain("Filed ✓"));
  });

  it("a date change refetches the status for the NEW date", async () => {
    const { container } = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(fetchDailyFormStatus).toHaveBeenCalledWith("JOB-A", TODAY));
    fireEvent.change(dateInput(container), { target: { value: "2026-01-15" } });
    await waitFor(() => expect(fetchDailyFormStatus).toHaveBeenCalledWith("JOB-A", "2026-01-15"));
  });
});

describe("DailyReportTab — filed banner + amend + submit", () => {
  const FILED = { filed_at: 1_700_000_000, filed_by_name: "Mo Manager" };

  it("shows the 'Daily report filed ✓' banner when daily_filed is set, with the form still open today", async () => {
    vi.mocked(fetchDailyFormStatus).mockResolvedValue({ filed: { "daily-report": FILED }, daily_filed: FILED });
    const { container, getByLabelText } = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(container.textContent ?? "").toContain("Daily report filed ✓"));
    expect(container.textContent ?? "").toContain("by Mo Manager");
    // Today: the form renders OPEN below the banner (file-another / amend stays one tap away).
    expect(container.querySelector("details")).toBeNull();
    expect(getByLabelText("Submit daily report")).not.toBeNull();
  });

  it("Load & amend seeds the prior values and submits WITH amends_uuid (the existing amend machinery)", async () => {
    vi.mocked(fetchDailyFormStatus).mockResolvedValue({ filed: { "daily-report": FILED }, daily_filed: FILED });
    vi.mocked(api.fetchRecent).mockResolvedValue({ submission_uuid: "prior-1", values: { weather: "Sunny" } });
    const { container, getByText, getByLabelText } = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    const amendBtn = await waitFor(() => getByText("Load & amend it"));
    fireEvent.click(amendBtn);
    expect(inputValues(container)).toContain("Sunny");
    const submit = getByLabelText("Submit daily report") as HTMLButtonElement;
    expect(submit.textContent).toContain("Submit amendment");
    fireEvent.click(submit);
    await waitFor(() =>
      expect(api.submitForm).toHaveBeenCalledWith(expect.objectContaining({ amends_uuid: "prior-1" })),
    );
  });

  it("submit goes through the standard send-free path with the fixed envelope + idempotent id", async () => {
    const { container, getByLabelText } = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(getByLabelText("Submit daily report")).not.toBeNull());
    fireEvent.click(getByLabelText("Submit daily report"));
    await waitFor(() =>
      expect(api.submitForm).toHaveBeenCalledWith(
        expect.objectContaining({
          job_id: "JOB-A",
          // Resolved from the catalog, not named. A hard-coded version turns every future cut
          // into an unrelated-looking test failure — v7 broke this line, and v6 broke whatever
          // it said before.
          form_code: formCatalog().find((p) => p.parent_form_code === "daily-report")!.form_code!,
          work_date: TODAY,
          amends_uuid: null,
          submission_uuid: expect.any(String),
        }),
      ),
    );
    // The D3 site-photos key rides the standard values payload (initialValues seeds
    // every header photo field to [] — the Worker skips empty photo arrays).
    const sent = vi.mocked(api.submitForm).mock.calls[0][0] as { values: Record<string, unknown> };
    expect(sent.values.site_photos).toEqual([]);
    // Success feedback + the status refetch that flips the indicators/banner.
    await waitFor(() => expect(container.textContent ?? "").toContain("Submitted ✓"));
    expect(vi.mocked(fetchDailyFormStatus).mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it("a submit failure shows the inline error and re-enables the button (never silent)", async () => {
    vi.mocked(api.submitForm).mockRejectedValue(new Error("Submission failed. Please try again."));
    const { container, getByLabelText } = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(getByLabelText("Submit daily report")).not.toBeNull());
    fireEvent.click(getByLabelText("Submit daily report"));
    await waitFor(() => expect(container.textContent ?? "").toContain("Submission failed."));
    expect((getByLabelText("Submit daily report") as HTMLButtonElement).disabled).toBe(false);
    expect(container.textContent ?? "").not.toContain("Submitted ✓");
  });

  it("a PAST date with a filing defaults to the filed state first: the form collapses behind a disclosure", async () => {
    vi.mocked(fetchDailyFormStatus).mockResolvedValue({ filed: { "daily-report": FILED }, daily_filed: FILED });
    const { container } = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(container.textContent ?? "").toContain("Daily report filed ✓"));
    fireEvent.change(dateInput(container), { target: { value: "2026-01-15" } });
    await waitFor(() => expect(container.querySelector("details")).not.toBeNull());
    const details = container.querySelector("details")!;
    expect(details.hasAttribute("open")).toBe(false); // filed state first; the form is one tap away
    expect(details.textContent ?? "").toContain("File another or amend for this date");
    expect(details.querySelector('[aria-label="Submit daily report"]')).not.toBeNull();
  });
});

describe("DailyReportTab — draft persistence (the deep-link data-loss BLOCK fix)", () => {
  beforeEach(() => sessionStorage.clear());

  it("typed values survive an unmount/remount (a form_link navigation can no longer destroy the day's work)", async () => {
    const first = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(first.container.textContent ?? "").toContain("SITE SUPERVISOR"));
    fireEvent.change(first.getByLabelText("Weather"), { target: { value: "Sunny, light wind" } });
    first.unmount(); // = the App page-node swap when a form_link deep-link fires
    const second = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => {
      expect((second.getByLabelText("Weather") as HTMLInputElement).value).toBe("Sunny, light wind"); // the draft WON over the prefill
    });
  });

  it("a successful submit clears the draft — the next mount starts fresh", async () => {
    const first = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(first.container.textContent ?? "").toContain("SITE SUPERVISOR"));
    fireEvent.change(first.getByLabelText("Weather"), { target: { value: "Overcast" } });
    fireEvent.click(first.getByLabelText("Submit daily report"));
    await waitFor(() => expect(api.submitForm).toHaveBeenCalled());
    first.unmount();
    const second = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(second.container.textContent ?? "").toContain("SITE SUPERVISOR"));
    expect((second.getByLabelText("Weather") as HTMLInputElement).value).toBe(""); // draft cleared on filing
  });

  // ── Optimization #1: the write path is debounced + photo-stripped. The unmount/remount test
  // above now exercises the FLUSH path (an unmount inside the debounce window persists the
  // pending write — the deep-link loss-moment); the per-date test below exercises the
  // key-change flush (a date switch mid-window must not drop the old date's keystrokes).
  it("draft writes are DEBOUNCED: no per-keystroke sessionStorage write; one window timer; the write carries the LATEST values", async () => {
    // Observed through sessionStorage CONTENT + the fake-timer queue (a Storage.prototype spy is
    // not reliably reached from inside the fake-timer tick in this jsdom setup — counts would lie).
    const KEY = `its-daily-draft:JOB-A:${TODAY}`;
    const view = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(view.container.textContent ?? "").toContain("SITE SUPERVISOR"));
    vi.useFakeTimers();
    try {
      fireEvent.change(view.getByLabelText("Weather"), { target: { value: "S" } });
      fireEvent.change(view.getByLabelText("Weather"), { target: { value: "Su" } });
      fireEvent.change(view.getByLabelText("Weather"), { target: { value: "Sunny" } });
      // The pre-fix bug: a full-values JSON.stringify + write on EVERY keystroke. Now: nothing
      // persisted yet, and the three keystrokes share ONE pending window timer (not one each).
      expect(sessionStorage.getItem(KEY)).toBeNull();
      expect(vi.getTimerCount()).toBe(1);
      vi.advanceTimersByTime(600);
      const raw = sessionStorage.getItem(KEY);
      expect(raw).not.toBeNull(); // the window elapsed -> the ONE debounced write landed...
      expect((JSON.parse(raw!) as Record<string, unknown>).weather).toBe("Sunny"); // ...with the latest, not the first
      expect(vi.getTimerCount()).toBe(0); // and nothing further is queued
    } finally {
      vi.useRealTimers();
    }
  });

  it("photo-typed keys are STRIPPED from the persisted draft (the base64 quota fix): text survives, photos re-seed empty", async () => {
    const first = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(first.container.textContent ?? "").toContain("SITE SUPERVISOR"));
    fireEvent.change(first.getByLabelText("Weather"), { target: { value: "Hazy" } });
    first.unmount(); // the unmount flush writes the draft
    await act(async () => {}); // flush React's (possibly deferred) passive unmount cleanup
    const raw = sessionStorage.getItem(`its-daily-draft:JOB-A:${TODAY}`);
    expect(raw).not.toBeNull();
    const draft = JSON.parse(raw!) as Record<string, unknown>;
    expect(draft.weather).toBe("Hazy");
    // The v5 site_photos header key is IN values (seeded []) but NEVER in the persisted draft —
    // the documented honest regression: attached-but-unsubmitted photos don't survive a restore.
    expect("site_photos" in draft).toBe(false);
    const second = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect((second.getByLabelText("Weather") as HTMLInputElement).value).toBe("Hazy"));
    expect(second.container.querySelector(".photo-field")).not.toBeNull(); // photo section intact, just empty
  });

  it("pagehide flushes the pending draft (R3-F9: mobile-Safari tab-kill never runs the unmount cleanup), and a second fire is a guarded no-op", async () => {
    const KEY = `its-daily-draft:JOB-A:${TODAY}`;
    const view = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(view.container.textContent ?? "").toContain("SITE SUPERVISOR"));
    fireEvent.change(view.getByLabelText("Weather"), { target: { value: "Drizzle" } });
    expect(sessionStorage.getItem(KEY)).toBeNull(); // still inside the debounce window — nothing persisted yet
    window.dispatchEvent(new Event("pagehide"));
    const raw = sessionStorage.getItem(KEY);
    expect(raw).not.toBeNull(); // the pagehide flush landed the pending write
    expect((JSON.parse(raw!) as Record<string, unknown>).weather).toBe("Drizzle");
    // Duplicate-guard: flushDraft nulls the pending ref before writing, so a second pagehide
    // (or the later unmount/debounce flush racing it) has nothing pending and writes nothing.
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    try {
      window.dispatchEvent(new Event("pagehide"));
      expect(setItem).not.toHaveBeenCalled();
    } finally {
      setItem.mockRestore();
    }
  });

  it("a quota-blown write is non-fatal: the flush swallows the error and the form keeps working", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("quota exceeded", "QuotaExceededError");
    });
    try {
      const view = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
      await waitFor(() => expect(view.container.textContent ?? "").toContain("SITE SUPERVISOR"));
      fireEvent.change(view.getByLabelText("Weather"), { target: { value: "Stormy" } });
      expect(() => view.unmount()).not.toThrow(); // the unmount flush hits the throwing write
      // React can DEFER the passive-effect unmount cleanup — flush it while the throwing spy is
      // still active, so the flush provably hit the quota error (review BLOCK: without this, the
      // deferred flush sometimes ran after mockRestore and silently persisted the draft).
      await act(async () => {});
    } finally {
      setItem.mockRestore();
    }
    // Belt-and-braces: no draft may survive into the second mount regardless of scheduling.
    sessionStorage.clear();
    // Worst case = pre-fix behavior (no draft), never a crash.
    const second = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(second.container.textContent ?? "").toContain("SITE SUPERVISOR"));
    expect((second.getByLabelText("Weather") as HTMLInputElement).value).toBe("");
  });

  it("drafts are per-date: switching the date swaps to that date's draft (or the seed), and back", async () => {
    const view = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    const { container } = view;
    await waitFor(() => expect(container.textContent ?? "").toContain("SITE SUPERVISOR"));
    const weather = () => view.getByLabelText("Weather") as HTMLInputElement;
    fireEvent.change(weather(), { target: { value: "Today's weather" } });
    fireEvent.change(dateInput(container), { target: { value: "2026-07-01" } });
    await waitFor(() => expect(weather().value).toBe("")); // yesterday starts from the seed
    fireEvent.change(weather(), { target: { value: "Yesterday's weather" } });
    fireEvent.change(dateInput(container), { target: { value: TODAY } });
    await waitFor(() => expect(weather().value).toBe("Today's weather")); // each day kept its own
  });
});

describe("DailyReportTab — photos must survive the draft cycle (live field bug 2026-07-03)", () => {
  beforeEach(() => sessionStorage.clear());

  /** jsdom has no canvas/createImageBitmap — stub the minimal encode surface so the REAL
   *  encodePhoto downscale ladder runs end-to-end. `blobBytes` sizes every canvas.toBlob
   *  result (≤ PHOTO_MAX_BYTES ⇒ first rung succeeds). Returns a restore fn. */
  function stubPhotoEncode(blobBytes: number): () => void {
    const g = globalThis as { createImageBitmap?: unknown };
    const hadCIB = "createImageBitmap" in g;
    const prevCIB = g.createImageBitmap;
    g.createImageBitmap = vi.fn(async () => ({ width: 1600, height: 1200, close: vi.fn() }));
    const getContext = vi
      .spyOn(HTMLCanvasElement.prototype, "getContext")
      .mockImplementation(() => ({ drawImage: vi.fn() }) as unknown as CanvasRenderingContext2D);
    const toBlob = vi
      .spyOn(HTMLCanvasElement.prototype, "toBlob")
      .mockImplementation(function (cb: BlobCallback) {
        cb(new Blob([new Uint8Array(blobBytes)], { type: "image/jpeg" }));
      });
    return () => {
      getContext.mockRestore();
      toBlob.mockRestore();
      if (hadCIB) g.createImageBitmap = prevCIB;
      else delete g.createImageBitmap;
    };
  }

  it("REGRESSION: a wake-refresh (refreshToken bump) never wipes an attached photo via the photo-stripped draft", async () => {
    // THE live sequence: the file picker itself blurs the window; picking the photo closes it →
    // focus/visibilitychange → FieldOpsMyTasks.onWake → refreshAll() → refreshToken bump → the
    // prefill effect re-fires, fetchJobDetail resolves, and the (photo-STRIPPED) sessionStorage
    // draft is re-applied over live values — pre-fix, initialValues re-seeded site_photos to []
    // and the photo "flashed for a second and then disappeared".
    const restore = stubPhotoEncode(1_000); // well under PHOTO_MAX_BYTES → first ladder rung wins
    try {
      const view = render(
        <DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} refreshToken={0} />,
      );
      await waitFor(() => expect(view.container.textContent ?? "").toContain("SITE SUPERVISOR"));
      // 1. Typed work arms the draft machinery.
      fireEvent.change(view.getByLabelText("Weather"), { target: { value: "Sunny" } });
      // 2. Attach a photo through the REAL input → the REAL encodePhoto ladder (stubbed canvas).
      const input = view.container.querySelector(
        '[data-testid="photo-input-site_photos"]',
      ) as HTMLInputElement;
      fireEvent.change(input, {
        target: { files: [new File([new Uint8Array(5_000)], "site.jpg", { type: "image/jpeg" })] },
      });
      await waitFor(() => expect(view.container.querySelector(".photo-field__thumb img")).not.toBeNull());
      // 3. The debounce window elapses — the photo-stripped draft is now persisted.
      await act(async () => {
        await new Promise((r) => setTimeout(r, 600));
      });
      const raw = sessionStorage.getItem(`its-daily-draft:JOB-A:${TODAY}`);
      expect(raw).not.toBeNull();
      expect("site_photos" in (JSON.parse(raw!) as Record<string, unknown>)).toBe(false);
      // 4. The picker-close focus event: the parent bumps refreshToken (FieldOpsMyTasks.onWake).
      view.rerender(
        <DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} refreshToken={1} />,
      );
      // 5. The re-fired prefill effect resolves and applies the draft.
      await waitFor(() => expect(vi.mocked(jobs.fetchJobDetail)).toHaveBeenCalledTimes(2));
      await act(async () => {});
      // The photo SURVIVES (live values win over the photo-stripped draft for photo keys)…
      expect(view.container.querySelector(".photo-field__thumb img")).not.toBeNull();
      expect(view.container.textContent ?? "").toContain("(1/4)");
      // …and the typed work survives too (the draft carried it).
      expect((view.getByLabelText("Weather") as HTMLInputElement).value).toBe("Sunny");
      // …and the photo still files with the submission.
      fireEvent.click(view.getByLabelText("Submit daily report"));
      await waitFor(() => expect(api.submitForm).toHaveBeenCalled());
      const sent = vi.mocked(api.submitForm).mock.calls[0][0] as { values: Record<string, unknown> };
      expect((sent.values.site_photos as unknown[]).length).toBe(1);
    } finally {
      restore();
    }
  });
});

describe("DailyReportTab — per-job requirements (slice D4)", () => {
  const REQS: DailyRequirementItem[] = [
    { id: 1, seq: 10, kind: "note", label: "Client requires FR clothing", form_code: null, options: null },
    { id: 2, seq: 20, kind: "confirm", label: "Badge in at the client gate", form_code: null, options: null },
    { id: 3, seq: 30, kind: "text", label: "Client rep spoken to today", form_code: null, options: null },
  ];
  beforeEach(() => sessionStorage.clear());

  it("fetches the job's items and renders them inside the form's Job-specific requirements section", async () => {
    vi.mocked(fetchDailyRequirements).mockResolvedValue(REQS);
    const { container, getByLabelText } = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(container.textContent ?? "").toContain("Job-specific requirements"));
    expect(fetchDailyRequirements).toHaveBeenCalledWith("JOB-A");
    expect(container.textContent ?? "").toContain("Client requires FR clothing");
    expect((getByLabelText("Badge in at the client gate") as HTMLInputElement).type).toBe("checkbox");
  });

  it("zero items → the section renders nothing (the base form is unaffected)", async () => {
    const { container } = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(container.textContent ?? "").toContain("SITE SUPERVISOR"));
    expect(container.textContent ?? "").not.toContain("Job-specific requirements");
  });

  it("a requirements fetch failure soft-warns with a WORKING Retry — the form stays fillable (never silent)", async () => {
    vi.mocked(fetchDailyRequirements)
      .mockRejectedValueOnce(new ApiError(null, 500))
      .mockResolvedValue(REQS);
    const { container, getByLabelText } = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    // R1 convention: the ApiError's HUMAN copy surfaces (errMsg falls back only on non-Errors);
    // the `what`-scoped Retry disambiguates it from any sibling error.
    await waitFor(() => expect(getByLabelText("Retry loading job-specific requirements")).not.toBeNull());
    expect(container.textContent ?? "").toContain("Something went wrong on the server");
    expect((getByLabelText("Submit daily report") as HTMLButtonElement).disabled).toBe(false); // still fillable
    fireEvent.click(getByLabelText("Retry loading job-specific requirements"));
    await waitFor(() => expect(container.textContent ?? "").toContain("Job-specific requirements"));
  });

  it("a zero-interaction submit STILL files the seeded self-describing array (what was displayed)", async () => {
    vi.mocked(fetchDailyRequirements).mockResolvedValue(REQS);
    const { container, getByLabelText } = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(container.textContent ?? "").toContain("Job-specific requirements"));
    fireEvent.click(getByLabelText("Submit daily report"));
    await waitFor(() => expect(api.submitForm).toHaveBeenCalled());
    const payload = vi.mocked(api.submitForm).mock.calls[0][0];
    expect((payload.values as Record<string, unknown>).job_requirements).toEqual(
      REQS.map((r) => ({ label: r.label, kind: r.kind, response: "" })),
    );
  });

  it("answers file with the submission and the draft machinery covers them across an unmount", async () => {
    vi.mocked(fetchDailyRequirements).mockResolvedValue(REQS);
    const first = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(first.container.textContent ?? "").toContain("Job-specific requirements"));
    fireEvent.click(first.getByLabelText("Badge in at the client gate"));
    fireEvent.change(first.getByLabelText("Client rep spoken to today"), { target: { value: "Ana R." } });
    first.unmount(); // = a form_link deep-link navigation
    const second = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() =>
      expect((second.getByLabelText("Client rep spoken to today") as HTMLInputElement).value).toBe("Ana R."),
    );
    expect((second.getByLabelText("Badge in at the client gate") as HTMLInputElement).checked).toBe(true);
    // …and the restored answers file with the submission.
    fireEvent.click(second.getByLabelText("Submit daily report"));
    await waitFor(() => expect(api.submitForm).toHaveBeenCalled());
    const payload = vi.mocked(api.submitForm).mock.calls[0][0];
    const arr = (payload.values as Record<string, unknown>).job_requirements as
      { label: string; kind: string; response: string }[];
    expect(arr).toEqual([
      { label: "Client requires FR clothing", kind: "note", response: "" },
      { label: "Badge in at the client gate", kind: "confirm", response: "Confirmed" },
      { label: "Client rep spoken to today", kind: "text", response: "Ana R." },
    ]);
  });
});

describe("DailyReportTab — expected-materials receipt flow (Material receipts M2)", () => {
  const PENDING: ExpectedMaterialRow = {
    id: 11, material_id: 7, material_name: "Q.PEAK DUO", description: null,
    qty: 40, unit: "panels", expected_date: "2026-07-10", status: "expected",
    received_at: null, received_by_name: null, qty_received: null, note: null, seq: 10,
    line_uuid: "line-uuid-qpeak-11", part_number: null, category: null, expected_ship_date: null, receipt_status: null, qty_received_total: null,
  };
  beforeEach(() => sessionStorage.clear());

  // ── v7 day snapshot ────────────────────────────────────────────────────────────────────
  it("files the day's expected-materials SNAPSHOT with the submission (the v7 inversion)", async () => {
    vi.mocked(fetchExpectedMaterials).mockResolvedValue({
      expected_materials: [{ ...PENDING, part_number: "7000153", qty_received_total: null }],
      shipments: [], receipt_events: [], project_name: "Deep Lake",
    });
    const { getByLabelText, container } = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(container.textContent ?? "").toContain("Q.PEAK DUO"));
    // The row RENDERING and the snapshot being seeded into form state are two separate commits;
    // clicking straight after the text appears races the seeding effect (passes solo, fails under
    // full-suite timing). Flush pending effects first.
    await act(async () => {});
    fireEvent.click(getByLabelText("Submit daily report"));
    await waitFor(() => expect(api.submitForm).toHaveBeenCalled());
    const sent = vi.mocked(api.submitForm).mock.calls[0][0] as { values: Record<string, unknown> };
    expect(sent.values.expected_materials_receipt).toEqual([
      {
        material: "Q.PEAK DUO",
        part_number: "7000153",
        expected: "40 panels",
        status: "Expected",
        received: "",
      },
    ]);
  });

  it("the snapshot is NOT seeded when the materials load fails (files nothing, not an empty list)", async () => {
    // An empty array would assert "this job had no expected materials", which is a different and
    // false claim from "we could not read them".
    vi.mocked(fetchExpectedMaterials).mockRejectedValue(new Error("nope"));
    const { getByLabelText } = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(getByLabelText("Submit daily report")).not.toBeNull());
    fireEvent.click(getByLabelText("Submit daily report"));
    await waitFor(() => expect(api.submitForm).toHaveBeenCalled());
    const sent = vi.mocked(api.submitForm).mock.calls[0][0] as { values: Record<string, unknown> };
    expect("expected_materials_receipt" in sent.values).toBe(false);
  });

  it("seeding the snapshot does NOT dirty the form (no draft is persisted from it alone)", async () => {
    // setValues, never editValues: editValues sets dirtyRef, which gates draft persistence. A
    // snapshot-only seed writing a draft would resurrect a report the manager never typed.
    //
    // Asserted through UNMOUNT, which is what flushes the debounced draft write — checking
    // sessionStorage while still mounted proves nothing, because the timer has not fired yet.
    vi.mocked(fetchExpectedMaterials).mockResolvedValue({ expected_materials: [PENDING], shipments: [], receipt_events: [], project_name: "Deep Lake" });
    const view = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(view.container.textContent ?? "").toContain("Q.PEAK DUO"));
    view.unmount();
    const drafts = Object.keys(sessionStorage).filter((k) => k.startsWith("its-daily-draft:"));
    expect(drafts).toEqual([]);
  });

  it("fetches the job's expected materials and renders the pending row inside the form", async () => {
    vi.mocked(fetchExpectedMaterials).mockResolvedValue({ expected_materials: [PENDING], shipments: [], receipt_events: [], project_name: "Deep Lake" });
    const { container, getByLabelText } = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(container.textContent ?? "").toContain("Q.PEAK DUO"));
    expect(fetchExpectedMaterials).toHaveBeenCalledWith("JOB-A");
    expect(container.textContent ?? "").toContain("Expected materials");
    expect(getByLabelText("Confirm receipt of Q.PEAK DUO")).not.toBeNull();
    expect(getByLabelText("Report a problem with Q.PEAK DUO")).not.toBeNull();
  });

  it("Confirm receipt fires the M1 receive route, flips the pill, and APPENDS a Deliveries Received row", async () => {
    vi.mocked(fetchExpectedMaterials).mockResolvedValue({ expected_materials: [PENDING], shipments: [], receipt_events: [], project_name: "Deep Lake" });
    const { container, getByLabelText } = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(container.textContent ?? "").toContain("Q.PEAK DUO"));
    fireEvent.click(getByLabelText("Confirm receipt of Q.PEAK DUO"));
    await waitFor(() => expect(receiveExpectedMaterial).toHaveBeenCalledWith(11));
    // Optimistic flip: the pending actions are gone, the Received record line shows.
    await waitFor(() =>
      expect(container.querySelector('[aria-label="Confirm receipt of Q.PEAK DUO"]')).toBeNull(),
    );
    expect(container.textContent ?? "").toContain("Received");
    // The receipt landed IN the form: a deliveries_received row (item / condition / notes).
    const vals = inputValues(container);
    expect(vals).toContain("Q.PEAK DUO");
    expect(vals).toContain("Received OK");
    expect(vals).toContain("qty 40 panels");
    // …and it files with the submission.
    fireEvent.click(getByLabelText("Submit daily report"));
    await waitFor(() => expect(api.submitForm).toHaveBeenCalled());
    const payload = vi.mocked(api.submitForm).mock.calls[0][0] as { values: Record<string, unknown> };
    expect(payload.values.deliveries_received).toEqual([
      { item_material: "Q.PEAK DUO", condition: "Received OK", notes: "qty 40 panels" },
    ]);
    // v7 INVERTS the v5/v6 contract: the section now DOES file the day's snapshot. Until v7 this
    // asserted the key's ABSENCE, on the reasoning that reprinting a mutable list would snapshot
    // state the submission never carried. That is backwards for a document of record — the report
    // is signed against a delivery state that every later mark rewrites, so NOT capturing it was
    // the data loss. Deliberately rewritten, not worked around.
    expect(Array.isArray(payload.values.expected_materials_receipt)).toBe(true);
    // The append is draft-persisted like typed work (deep-link navigation loses nothing) —
    // asserted against the pre-submit write (submit clears the draft).
  });

  it("the Confirm-receipt append is draft-persisted (survives an unmount like typed work)", async () => {
    vi.mocked(fetchExpectedMaterials).mockResolvedValue({ expected_materials: [PENDING], shipments: [], receipt_events: [], project_name: "Deep Lake" });
    const first = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(first.container.textContent ?? "").toContain("Q.PEAK DUO"));
    fireEvent.click(first.getByLabelText("Confirm receipt of Q.PEAK DUO"));
    await waitFor(() => expect(inputValues(first.container)).toContain("Received OK"));
    first.unmount(); // = the material-incident deep-link page-node swap
    const second = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(inputValues(second.container)).toContain("Received OK"));
    expect(inputValues(second.container)).toContain("Q.PEAK DUO");
  });

  it("Report a problem prompts for the REQUIRED note, flags the row, and deep-links material-incident prefilled", async () => {
    vi.mocked(fetchExpectedMaterials).mockResolvedValue({ expected_materials: [PENDING], shipments: [], receipt_events: [], project_name: "Deep Lake" });
    const promptSpy = vi.spyOn(window, "prompt").mockReturnValue("Crushed corner on 3 pallets");
    const onOpenForm = vi.fn();
    const { container, getByLabelText } = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={onOpenForm} />);
    await waitFor(() => expect(container.textContent ?? "").toContain("Q.PEAK DUO"));
    fireEvent.click(getByLabelText("Report a problem with Q.PEAK DUO"));
    await waitFor(() =>
      expect(flagExpectedMaterialIncident).toHaveBeenCalledWith(11, "Crushed corner on 3 pallets"),
    );
    // The deep-link rides openForm with the R5 prefill values (description + expected qty), and
    // M3 Slice 1 carries the flagged line's stable key so the incident REFERENCES this M2 line.
    await waitFor(() =>
      expect(onOpenForm).toHaveBeenCalledWith(
        expect.objectContaining({
          jobId: "JOB-A",
          parentCode: "material-incident",
          workDate: TODAY,
          values: { material_description: "Q.PEAK DUO", qty_expected: "40", line_uuid: "line-uuid-qpeak-11" },
        }),
      ),
    );
    // Optimistic flip to the incident record.
    expect(container.textContent ?? "").toContain("Flagged");
    promptSpy.mockRestore();
  });

  it("M3 Slice 1 — a row with a NULL line_uuid deep-links WITHOUT a line_uuid value (a valid unlinked incident)", async () => {
    vi.mocked(fetchExpectedMaterials).mockResolvedValue({
      expected_materials: [{ ...PENDING, line_uuid: null }],
      shipments: [], project_name: "Deep Lake",
      receipt_events: [],
    });
    const promptSpy = vi.spyOn(window, "prompt").mockReturnValue("Short shipment");
    const onOpenForm = vi.fn();
    const { container, getByLabelText } = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={onOpenForm} />);
    await waitFor(() => expect(container.textContent ?? "").toContain("Q.PEAK DUO"));
    fireEvent.click(getByLabelText("Report a problem with Q.PEAK DUO"));
    await waitFor(() => expect(onOpenForm).toHaveBeenCalled());
    const prefill = onOpenForm.mock.calls[0][0] as { values: Record<string, unknown> };
    expect(prefill.values).toEqual({ material_description: "Q.PEAK DUO", qty_expected: "40" });
    expect("line_uuid" in prefill.values).toBe(false);
    promptSpy.mockRestore();
  });

  it("a cancelled prompt does nothing; an empty note errors WITHOUT flagging (the note is required)", async () => {
    vi.mocked(fetchExpectedMaterials).mockResolvedValue({ expected_materials: [PENDING], shipments: [], receipt_events: [], project_name: "Deep Lake" });
    const promptSpy = vi.spyOn(window, "prompt").mockReturnValue(null);
    const onOpenForm = vi.fn();
    const { container, getByLabelText } = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={onOpenForm} />);
    await waitFor(() => expect(container.textContent ?? "").toContain("Q.PEAK DUO"));
    fireEvent.click(getByLabelText("Report a problem with Q.PEAK DUO"));
    expect(flagExpectedMaterialIncident).not.toHaveBeenCalled();
    expect(onOpenForm).not.toHaveBeenCalled();
    promptSpy.mockReturnValue("   "); // whitespace-only = no note
    fireEvent.click(getByLabelText("Report a problem with Q.PEAK DUO"));
    await waitFor(() =>
      expect(container.textContent ?? "").toContain("A short note describing the problem is required."),
    );
    expect(flagExpectedMaterialIncident).not.toHaveBeenCalled();
    promptSpy.mockRestore();
  });

  it("a failed receive surfaces the action error inline and does NOT flip or append (never silent)", async () => {
    vi.mocked(fetchExpectedMaterials).mockResolvedValue({ expected_materials: [PENDING], shipments: [], receipt_events: [], project_name: "Deep Lake" });
    vi.mocked(receiveExpectedMaterial).mockRejectedValue(new Error("Already received by someone else."));
    const { container, getByLabelText } = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(container.textContent ?? "").toContain("Q.PEAK DUO"));
    fireEvent.click(getByLabelText("Confirm receipt of Q.PEAK DUO"));
    await waitFor(() =>
      expect(container.textContent ?? "").toContain("Already received by someone else."),
    );
    expect(getByLabelText("Confirm receipt of Q.PEAK DUO")).not.toBeNull(); // still pending
    expect(inputValues(container)).not.toContain("Received OK"); // no append
  });

  it("a fetch failure soft-warns with a WORKING Retry — the form stays fillable (never silent)", async () => {
    vi.mocked(fetchExpectedMaterials)
      .mockRejectedValueOnce(new ApiError(null, 500))
      .mockResolvedValue({ expected_materials: [PENDING], shipments: [], receipt_events: [], project_name: "Deep Lake" });
    const { container, getByLabelText } = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    // R1 convention: the ApiError's HUMAN copy surfaces; the `what`-scoped Retry disambiguates
    // it from any sibling (status / requirements) warn.
    await waitFor(() => expect(getByLabelText("Retry loading expected materials")).not.toBeNull());
    expect((getByLabelText("Submit daily report") as HTMLButtonElement).disabled).toBe(false); // still fillable
    // No section rendered while the read is failed — never a lying empty state.
    expect(container.querySelector(".fr__expected-materials")).toBeNull();
    fireEvent.click(getByLabelText("Retry loading expected materials"));
    await waitFor(() => expect(container.textContent ?? "").toContain("Q.PEAK DUO"));
  });

  it("zero expected materials → the explicit empty copy inside the section", async () => {
    const { container } = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(container.textContent ?? "").toContain("SITE SUPERVISOR"));
    await waitFor(() =>
      expect(container.textContent ?? "").toContain("No expected materials for this job."),
    );
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Directive 2026-07-03 (#2) — the SOP form's "Confirmed" buttons are a TRUE TOGGLE: clicking a
// selected scale option clears it back to unanswered. Exercised through the REAL daily-report-v5
// definition inside the tab (the operator's surface); the renderer-level matrix (multi-option
// scales, D4 confirm checkbox) lives in src/forms/__tests__/scale-toggle.test.tsx.
// ─────────────────────────────────────────────────────────────────────────────
describe("DailyReportTab — confirm-toggle (unconfirm on second click)", () => {
  const ITEM_LABEL = "Arrived before the crew and completed the pre-work site walkthrough";
  beforeEach(() => sessionStorage.clear());

  function confirmedBtn(container: HTMLElement): HTMLButtonElement {
    const rg = within(container).getByRole("radiogroup", { name: ITEM_LABEL });
    return within(rg).getByRole("button", { name: "Confirmed" }) as HTMLButtonElement;
  }

  it("confirmed → click → unconfirmed, with the visual states reverting (aria-pressed + --on class)", async () => {
    const { container } = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(container.textContent ?? "").toContain("SITE SUPERVISOR"));
    const btn = confirmedBtn(container);
    expect(btn.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(btn);
    expect(btn.getAttribute("aria-pressed")).toBe("true");
    expect(btn.className).toContain("fr__scale-opt--on");
    fireEvent.click(btn); // the un-confirm the operator asked for
    expect(btn.getAttribute("aria-pressed")).toBe("false");
    expect(btn.className).not.toContain("fr__scale-opt--on");
  });

  it("the toggled-off state FILES as the unanswered value shape (response '' — a string, key kept)", async () => {
    const { container, getByLabelText } = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(container.textContent ?? "").toContain("SITE SUPERVISOR"));
    fireEvent.click(confirmedBtn(container));
    fireEvent.click(confirmedBtn(container));
    fireEvent.click(getByLabelText("Submit daily report"));
    await waitFor(() => expect(api.submitForm).toHaveBeenCalled());
    const payload = vi.mocked(api.submitForm).mock.calls[0][0] as { values: Record<string, unknown> };
    // The payload shape is the pre-toggle contract: ChecklistState with a STRING response — ""
    // is the established unanswered value (the PDF renderer's blank cell, distinct from N/A).
    expect(payload.values.arrival).toEqual({ arrived_walkthrough: { response: "" } });
  });

  it("the draft survives the toggle round-trip across an unmount (confirm → unconfirm persists as unanswered)", async () => {
    const first = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(first.container.textContent ?? "").toContain("SITE SUPERVISOR"));
    fireEvent.click(confirmedBtn(first.container)); // confirm (dirty → draft machinery engages)
    fireEvent.click(confirmedBtn(first.container)); // unconfirm
    first.unmount(); // flushes the pending draft (the deep-link loss-moment)
    const second = render(<DailyReportTab linked={true} placement={PLACED} onOpenForm={vi.fn()} />);
    await waitFor(() => expect(second.container.textContent ?? "").toContain("SITE SUPERVISOR"));
    expect(confirmedBtn(second.container).getAttribute("aria-pressed")).toBe("false"); // restored unconfirmed
  });
});
