/**
 * The Archive / Un-archive panel (ROADMAP Track 6).
 *
 * The behaviours under test are the ones that keep the panel HONEST about a relocation it does not
 * itself perform. Pressing Archive records intent; the Mac-side pass moves six containers across
 * two external systems and reports back. So the panel must never claim completion from a 200, must
 * name which containers are stuck when only some moved, and must render nothing at all for a viewer
 * without `cap.job.archive` — the previous dropdown's sin was telling the operator a job was
 * archived the instant a flag flipped.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/fieldops_jobtracker", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/fieldops_jobtracker")>();
  return { ...actual, archiveJob: vi.fn(), unarchiveJob: vi.fn() };
});

import * as api from "../../lib/fieldops_jobtracker";
import type { JobArchiveStatus, JobDetail } from "../../lib/fieldops_jobtracker";
import { JobArchivePanel } from "../JobArchivePanel";

afterEach(cleanup);
beforeEach(() => vi.clearAllMocks());

const CONTAINERS = [
  { key: "smartsheet:safety", label: "Safety", moved: true, note: "" },
  { key: "smartsheet:progress", label: "Progress", moved: true, note: "" },
  { key: "smartsheet:purchase_orders", label: "Purchase Orders", moved: true, note: "" },
  { key: "smartsheet:subcontracts", label: "Subcontracts", moved: true, note: "" },
  { key: "box:safety", label: "Safety", moved: true, note: "" },
  { key: "box:progress", label: "Progress", moved: true, note: "" },
];

function archiveBlock(over: Partial<JobArchiveStatus> = {}): JobArchiveStatus {
  return {
    state: "none",
    direction: "",
    requested_at: null,
    completed_at: null,
    attempts: 0,
    containers: [],
    ...over,
  } as JobArchiveStatus;
}

function job(archive: JobArchiveStatus | null): JobDetail {
  return { job_id: "JOB-000017", project_name: "Coker", archive } as unknown as JobDetail;
}

function show(archive: JobArchiveStatus | null, canArchive = true) {
  const onChanged = vi.fn();
  const utils = render(
    <JobArchivePanel job={job(archive)} canArchive={canArchive} onChanged={onChanged} />,
  );
  return { onChanged, ...utils };
}

describe("JobArchivePanel — who sees it at all", () => {
  it("renders nothing without cap.job.archive", () => {
    const { container } = show(archiveBlock(), false);
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing when the worker withheld the archive block", () => {
    // The capability gate is server-side: a viewer without the cap gets `archive: null`, so the
    // panel must handle absence rather than assume the prop is populated.
    const { container } = show(null, true);
    expect(container.firstChild).toBeNull();
  });

  it("offers Archive on a job that has never entered the workflow", () => {
    show(archiveBlock({ state: "none" }));
    expect(screen.getByRole("button", { name: /archive job/i })).toBeTruthy();
  });
});

describe("JobArchivePanel — it never claims more than it knows", () => {
  it("says 'waiting to be picked up' before the daemon has reported anything", () => {
    // requested + zero containers = the queue row exists and nothing has run. Saying "0 of 6 moved"
    // here would imply the pass ran and failed.
    show(archiveBlock({ state: "requested", direction: "archive", containers: [] }));
    expect(screen.getByRole("status").textContent).toMatch(/waiting/i);
  });

  it("reports progress as N of M while in flight, without claiming completion", () => {
    show(archiveBlock({
      state: "in_progress",
      direction: "archive",
      containers: CONTAINERS.map((c, i) => ({ ...c, moved: i < 4 })),
    }));
    const banner = screen.getByRole("status").textContent ?? "";
    expect(banner).toMatch(/4 of 6/);
    expect(banner).toMatch(/archiving/i);
  });

  it("says Un-archiving, not Archiving, when the direction is reversed", () => {
    show(archiveBlock({ state: "in_progress", direction: "unarchive", containers: CONTAINERS }));
    expect(screen.getByRole("status").textContent).toMatch(/un-archiving/i);
  });

  it("only offers Un-archive once the archive actually completed", () => {
    show(archiveBlock({ state: "complete", direction: "archive", containers: CONTAINERS }));
    expect(screen.getByRole("button", { name: /un-archive/i })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^archive job…$/i })).toBeNull();
  });
});

describe("JobArchivePanel — a partial must be legible without opening logs", () => {
  it("NAMES the containers that did not move, with their reason", () => {
    // "4 of 6 moved" alone is not actionable — a Tier-2 operator has to know WHICH folder in WHICH
    // system is stuck. This is the whole reason the daemon reports per-container detail.
    show(archiveBlock({
      state: "partial",
      direction: "archive",
      containers: [
        ...CONTAINERS.slice(0, 4),
        { key: "box:safety", label: "Safety", moved: false, note: "BoxError" },
        { key: "box:progress", label: "Progress", moved: false, note: "BoxError" },
      ],
    }));
    const alert = screen.getByRole("alert").textContent ?? "";
    expect(alert).toMatch(/4 of 6/);
    expect(alert).toMatch(/did not move/i);
    expect(alert).toMatch(/BoxError/);
  });

  it("distinguishes 'nothing moved' from a partial", () => {
    show(archiveBlock({
      state: "failed",
      direction: "archive",
      containers: CONTAINERS.map((c) => ({ ...c, moved: false })),
    }));
    expect(screen.getByRole("alert").textContent).toMatch(/nothing moved/i);
  });

  it("tells the operator NOT to drag folders by hand", () => {
    // The failure mode this copy exists to prevent: a manual fix that desynchronises the recorded
    // folder ids from where the folders actually are.
    show(archiveBlock({ state: "failed", direction: "archive", containers: CONTAINERS.map((c) => ({ ...c, moved: false })) }));
    expect(screen.getByRole("alert").textContent).toMatch(/do not drag/i);
  });

  it("offers Try again on a partial", () => {
    show(archiveBlock({ state: "partial", direction: "archive", containers: CONTAINERS }));
    expect(screen.getByRole("button", { name: /try again/i })).toBeTruthy();
  });

  it.each(["partial", "failed"] as const)(
    "tells the operator a %s does NOT retry on its own",
    (state) => {
      // The queue route serves only archive_state IN ('requested','in_progress'), so 'partial'
      // and 'failed' are TERMINAL for the daemon — the row leaves the queue and nothing picks it
      // up again. This copy previously said "The system retries automatically", which sent the
      // operator away from the one control that resumes it while their job sat half-relocated
      // across two external systems.
      show(archiveBlock({ state, direction: "archive", containers: CONTAINERS.map((c) => ({ ...c, moved: false })) }));
      const alert = screen.getByRole("alert").textContent ?? "";
      expect(alert).toMatch(/will not retry on its own/i);
      expect(alert).not.toMatch(/retries automatically/i);
    },
  );
});

describe("JobArchivePanel — the request is gated on the typed confirmation", () => {
  it("does not call the API until the exact project name is typed", async () => {
    show(archiveBlock({ state: "none" }));
    fireEvent.click(screen.getByRole("button", { name: /archive job…/i }));

    const input = screen.getByLabelText("Confirmation phrase");
    fireEvent.change(input, { target: { value: "Cok" } });
    fireEvent.submit(input.closest("form")!);
    expect(api.archiveJob).not.toHaveBeenCalled();

    fireEvent.change(input, { target: { value: "Coker" } });
    fireEvent.submit(input.closest("form")!);
    await waitFor(() => expect(api.archiveJob).toHaveBeenCalledWith("JOB-000017", "Coker"));
  });

  it("surfaces a refusal instead of leaving the operator with a silent dead button", async () => {
    vi.mocked(api.archiveJob).mockRejectedValueOnce(new Error("That job is already archived."));
    show(archiveBlock({ state: "none" }));
    fireEvent.click(screen.getByRole("button", { name: /archive job…/i }));
    const input = screen.getByLabelText("Confirmation phrase");
    fireEvent.change(input, { target: { value: "Coker" } });
    fireEvent.submit(input.closest("form")!);

    await waitFor(() =>
      expect(screen.getAllByRole("alert").some((n) => /already archived/i.test(n.textContent ?? ""))).toBe(true),
    );
  });

  it("routes the un-archive button to the un-archive endpoint, not archive", async () => {
    show(archiveBlock({ state: "complete", direction: "archive", containers: CONTAINERS }));
    fireEvent.click(screen.getByRole("button", { name: /un-archive/i }));
    const input = screen.getByLabelText("Confirmation phrase");
    fireEvent.change(input, { target: { value: "Coker" } });
    fireEvent.submit(input.closest("form")!);
    await waitFor(() => expect(api.unarchiveJob).toHaveBeenCalledWith("JOB-000017", "Coker"));
    expect(api.archiveJob).not.toHaveBeenCalled();
  });
});
