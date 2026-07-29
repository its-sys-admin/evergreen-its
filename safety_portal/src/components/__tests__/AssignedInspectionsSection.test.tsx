/**
 * AssignedInspectionsSection (R2 extraction) — section-level detail tests: never-silent load
 * states, template_title heading, overdue treatment, humanized labels, completed collapse, and the
 * mutation/refetch try-split. Page-level integration lives in pages/__tests__/FieldOpsMyTasks.test.tsx.
 */
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/fieldops_checklist", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/fieldops_checklist")>();
  return { ...actual, fetchAssignedInspections: vi.fn(), completeChecklistItem: vi.fn(), uncompleteChecklistItem: vi.fn(), recordCountItem: vi.fn(), submitChecklistCompletion: vi.fn() };
});
vi.mock("../../lib/auth", () => ({ useAuth: vi.fn() }));

import * as checklist from "../../lib/fieldops_checklist";
import { ApiError } from "../../lib/errorCopy";
import { useAuth } from "../../lib/auth";
import { AssignedInspectionsSection } from "../AssignedInspectionsSection";

// #17 — the "Sign & log to progress report" action is gated on this site-wide flag (useAuth).
// Default OFF so every legacy test renders EXACTLY as before (the feature is DARK).
function setProgressFlag(on: boolean) {
  vi.mocked(useAuth).mockReturnValue({
    user: { username: "sam", role: "manager", capabilities: ["cap.tasks.own"], checklist_progress_logging_enabled: on },
    loading: false,
    login: vi.fn(),
    logout: vi.fn(),
  });
}

afterEach(cleanup);
beforeEach(() => {
  vi.resetAllMocks();
  setProgressFlag(false);
});

const ITEM: checklist.ChecklistItemState = { id: 40, source_item_id: 1, item_type: "manual_attest", label: "Harness checked", form_code: null, target_count: null, status: "open", note: null, photo_ref: null, completed_by: null, completed_at: null, value_num: null, filed_by: null, photo_status: null, requires_photo: false };

function inspection(overrides: Partial<checklist.AssignedInstance> = {}, items: checklist.ChecklistItemState[] = [ITEM]): checklist.AssignedInspection {
  return {
    instance: { id: 30, job_id: "JOB-A", project_name: "Alpha", instance_date: "2099-07-10", status: "open", template_title: "Fall protection", created_at: 100, progress_logged: false, ...overrides },
    items,
  };
}

// jsdom's window.scrollTo is a not-implemented stub; the signature sheet's scroll
// lock restores the offset on close. Silence the noise.
window.scrollTo = () => {};

/**
 * Drive the SignaturePad to capture a non-empty SVG path so the Log button enables.
 *
 * The inline pad is now a read-only PREVIEW and a tap target — capture happens in a
 * full-screen sheet portaled to document.body, so the drawing surface is NOT inside
 * RTL's `container`. Open it, draw on the sheet's surface, then commit with Done.
 */
function signOn(container: HTMLElement) {
  fireEvent.click(container.querySelector('[aria-label="Open signature pad"]') as HTMLElement);

  const sheet = document.body.querySelector('[role="dialog"][aria-label="Sign"]');
  if (!sheet) throw new Error("signature sheet did not open");

  const pad = sheet.querySelector('[aria-label="Signature drawing surface"]') as SVGSVGElement;
  // jsdom reports an all-zero rect, which the pad refuses (dividing by it would emit
  // Infinity into the path). Give it a real 600x180 box so a true path is captured.
  pad.getBoundingClientRect = () =>
    ({ x: 0, y: 0, left: 0, top: 0, right: 600, bottom: 180, width: 600, height: 180 }) as DOMRect;

  fireEvent.pointerDown(pad, { clientX: 10, clientY: 10, pointerId: 1 });
  fireEvent.pointerMove(pad, { clientX: 20, clientY: 24, pointerId: 1 });
  fireEvent.pointerUp(pad, { clientX: 20, clientY: 24, pointerId: 1 });

  const done = Array.from(sheet.querySelectorAll("button")).find(
    (b) => b.textContent?.trim() === "Done",
  );
  if (!done) throw new Error("signature sheet has no Done button");
  fireEvent.click(done);
}

function respOk(inspections: checklist.AssignedInspection[]) {
  vi.mocked(checklist.fetchAssignedInspections).mockResolvedValue({ inspections, linked: true });
}

describe("AssignedInspectionsSection — load states (Mandatory B)", () => {
  it("shows a distinct loading state while the fetch is in flight", () => {
    vi.mocked(checklist.fetchAssignedInspections).mockReturnValue(new Promise(() => {}));
    const { container } = render(<AssignedInspectionsSection />);
    expect(container.textContent ?? "").toContain("Loading assigned inspections…");
  });

  it("a load failure shows the human error + a working Retry (previously an invisible section)", async () => {
    vi.mocked(checklist.fetchAssignedInspections)
      .mockRejectedValueOnce(new ApiError(null, 500))
      .mockResolvedValueOnce({ inspections: [inspection()], linked: true });
    const { container, getByLabelText } = render(<AssignedInspectionsSection />);
    await waitFor(() => expect(container.textContent ?? "").toContain("Something went wrong on the server"));
    fireEvent.click(getByLabelText("Retry loading assigned inspections"));
    await waitFor(() => expect(container.textContent ?? "").toContain("Fall protection"));
    expect(checklist.fetchAssignedInspections).toHaveBeenCalledTimes(2);
  });

  it("renders nothing on a CONFIRMED-empty response", async () => {
    respOk([]);
    const { container } = render(<AssignedInspectionsSection />);
    await waitFor(() => expect(checklist.fetchAssignedInspections).toHaveBeenCalled());
    await waitFor(() => expect(container.querySelector('[aria-label="Assigned inspections"]')).toBeNull());
    expect((container.textContent ?? "").trim()).toBe("");
  });
});

describe("AssignedInspectionsSection — headings + dates", () => {
  it("each inspection card is titled by template_title with #id demoted + humanized status", async () => {
    respOk([inspection()]);
    const { container } = render(<AssignedInspectionsSection />);
    await waitFor(() => expect(container.textContent ?? "").toContain("Fall protection"));
    const card = container.querySelector(".checklist-task-card")!;
    expect(card.querySelector(".dash-card__title")?.textContent).toBe("Fall protection");
    expect(card.textContent ?? "").toContain("#30"); // id demoted to the card sub-line
    expect(card.textContent ?? "").toContain("Open"); // labels.ts, not raw 'open'
    expect(card.textContent ?? "").toContain("due");
  });

  it("falls back to 'Inspection' when template_title is null (legacy instances)", async () => {
    respOk([inspection({ template_title: null })]);
    const { container } = render(<AssignedInspectionsSection />);
    await waitFor(() => expect(container.querySelector(".checklist-task-card")).not.toBeNull());
    expect(container.querySelector(".dash-card__title")!.textContent ?? "").toContain("Inspection");
  });

  it("an OPEN inspection past its due date gets an Overdue warn pill", async () => {
    respOk([inspection({ instance_date: "2020-01-01" })]);
    const { container } = render(<AssignedInspectionsSection />);
    await waitFor(() => expect(container.textContent ?? "").toContain("Overdue"));
    expect(container.querySelector(".dash-pill--warn")?.textContent).toBe("Overdue");
  });

  it("no Overdue pill when complete (even past due) or when due in the future", async () => {
    respOk([
      inspection({ id: 30, instance_date: "2020-01-01", status: "complete" }),
      inspection({ id: 31, instance_date: "2099-07-10", status: "open" }),
    ]);
    const { container } = render(<AssignedInspectionsSection />);
    await waitFor(() => expect(container.textContent ?? "").toContain("Fall protection"));
    expect(container.textContent ?? "").not.toContain("Overdue");
  });
});

describe("AssignedInspectionsSection — rows + try-split", () => {
  it("shows every item inline in an opened inspection — a done item keeps a visible Undo (toggle off)", async () => {
    respOk([
      inspection({}, [ITEM, { ...ITEM, id: 41, label: "Lanyard tagged", status: "done", completed_by: "sam", completed_at: 1 }]),
    ]);
    const { container, getByLabelText } = render(<AssignedInspectionsSection />);
    fireEvent.click(await waitFor(() => getByLabelText("Open Fall protection inspection")));
    // No collapse: both the open and the done item render in one list...
    await waitFor(() => expect(container.textContent ?? "").toContain("Lanyard tagged"));
    expect(container.querySelector("details.dash-completed")).toBeNull();
    expect(container.textContent ?? "").toContain("Harness checked");
    // ...and the done item exposes an Undo so the person can toggle it back open.
    expect(getByLabelText("Undo item 41")).not.toBeNull();
    expect(getByLabelText("Complete item 40")).not.toBeNull();
  });

  it("mutation success + refetch failure: success feedback, data kept, soft warn (never 'failed')", async () => {
    vi.mocked(checklist.fetchAssignedInspections)
      .mockResolvedValueOnce({ inspections: [inspection()], linked: true })
      .mockRejectedValue(new ApiError(null, 500)); // every refetch fails
    vi.mocked(checklist.completeChecklistItem).mockResolvedValue({ ok: true, id: 40, status: "done", instance_status: "complete" });
    const { getByLabelText, container } = render(<AssignedInspectionsSection />);
    fireEvent.click(await waitFor(() => getByLabelText("Open Fall protection inspection")));
    fireEvent.click(await waitFor(() => getByLabelText("Complete item 40")));
    await waitFor(() => expect(container.textContent ?? "").toContain("Inspection complete."));
    await waitFor(() => expect(container.textContent ?? "").toContain("Saved — but the list couldn't refresh"));
    expect(container.textContent ?? "").not.toContain("Update failed.");
    // The CompleteResult was applied locally: the item flipped to done (its Undo shows inline now) +
    // the instance reads complete.
    expect(getByLabelText("Undo item 40")).not.toBeNull();
    expect(container.textContent ?? "").toContain("Complete"); // humanized instance status
  });

  it("per-row busy: an in-flight completion disables only that row", async () => {
    respOk([inspection({}, [ITEM, { ...ITEM, id: 42, label: "Anchor point rated" }])]);
    vi.mocked(checklist.completeChecklistItem).mockReturnValue(new Promise(() => {})); // never settles
    const { getByLabelText } = render(<AssignedInspectionsSection />);
    fireEvent.click(await waitFor(() => getByLabelText("Open Fall protection inspection")));
    fireEvent.click(await waitFor(() => getByLabelText("Complete item 40")));
    await waitFor(() => expect((getByLabelText("Complete item 40") as HTMLButtonElement).disabled).toBe(true));
    expect((getByLabelText("Complete item 42") as HTMLButtonElement).disabled).toBe(false);
  });
});

describe("AssignedInspectionsSection — #17 sign & log to progress report", () => {
  const DONE_ITEM: checklist.ChecklistItemState = { ...ITEM, status: "done", completed_by: "sam", completed_at: 1 };
  const completeInsp = (over: Partial<checklist.AssignedInstance> = {}) =>
    inspection({ status: "complete", ...over }, [DONE_ITEM]);

  it("DARK (flag off): a COMPLETE inspection shows NO sign-and-log action", async () => {
    setProgressFlag(false);
    respOk([completeInsp()]);
    const { container, getByLabelText, queryByLabelText } = render(<AssignedInspectionsSection />);
    fireEvent.click(await waitFor(() => getByLabelText("Open Fall protection inspection")));
    await waitFor(() => expect(container.textContent ?? "").toContain("Harness checked"));
    expect(queryByLabelText("Sign and log this inspection to the progress report")).toBeNull();
    expect(container.textContent ?? "").not.toContain("Sign & log to progress report");
  });

  it("flag ON: the action shows only when COMPLETE (an OPEN inspection has none)", async () => {
    setProgressFlag(true);
    respOk([inspection({ status: "open" }, [ITEM])]);
    const { getByLabelText, queryByLabelText, container } = render(<AssignedInspectionsSection />);
    fireEvent.click(await waitFor(() => getByLabelText("Open Fall protection inspection")));
    await waitFor(() => expect(container.textContent ?? "").toContain("Harness checked"));
    expect(queryByLabelText("Sign and log this inspection to the progress report")).toBeNull();
  });

  it("flag ON + COMPLETE + not-yet-logged: captures a signature, calls submitChecklistCompletion, shows the pill", async () => {
    setProgressFlag(true);
    respOk([completeInsp()]);
    vi.mocked(checklist.submitChecklistCompletion).mockResolvedValue({ ok: true, submission_uuid: "u-1" });
    const { getByLabelText, container } = render(<AssignedInspectionsSection />);
    fireEvent.click(await waitFor(() => getByLabelText("Open Fall protection inspection")));
    // Open the sign panel.
    fireEvent.click(await waitFor(() => getByLabelText("Sign and log this inspection to the progress report")));
    // The Log button is disabled until a signature is captured.
    const logBtn = () => getByLabelText("Log to progress report") as HTMLButtonElement;
    await waitFor(() => expect(logBtn().disabled).toBe(true));
    signOn(container);
    await waitFor(() => expect(logBtn().disabled).toBe(false));
    fireEvent.click(logBtn());
    await waitFor(() => expect(checklist.submitChecklistCompletion).toHaveBeenCalledTimes(1));
    expect(vi.mocked(checklist.submitChecklistCompletion).mock.calls[0][0]).toBe(30); // instance id
    expect(typeof vi.mocked(checklist.submitChecklistCompletion).mock.calls[0][1]).toBe("string");
    // Success → the pill replaces the action (marked progress_logged locally).
    await waitFor(() => expect(container.textContent ?? "").toContain("Logged to progress report ✓"));
  });

  it("flag ON + already-logged: shows the pill, NOT the action", async () => {
    setProgressFlag(true);
    respOk([completeInsp({ progress_logged: true })]);
    const { getByLabelText, queryByLabelText, container } = render(<AssignedInspectionsSection />);
    fireEvent.click(await waitFor(() => getByLabelText("Open Fall protection inspection")));
    await waitFor(() => expect(container.textContent ?? "").toContain("Logged to progress report ✓"));
    expect(queryByLabelText("Sign and log this inspection to the progress report")).toBeNull();
  });

  it("flag ON + submit failure: shows the human error, never a false pill", async () => {
    setProgressFlag(true);
    respOk([completeInsp()]);
    vi.mocked(checklist.submitChecklistCompletion).mockRejectedValue(new ApiError("Already logged.", 409));
    const { getByLabelText, container } = render(<AssignedInspectionsSection />);
    fireEvent.click(await waitFor(() => getByLabelText("Open Fall protection inspection")));
    fireEvent.click(await waitFor(() => getByLabelText("Sign and log this inspection to the progress report")));
    signOn(container);
    await waitFor(() => expect((getByLabelText("Log to progress report") as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(getByLabelText("Log to progress report"));
    await waitFor(() => expect(container.textContent ?? "").toContain("Already logged."));
    expect(container.textContent ?? "").not.toContain("Logged to progress report ✓");
  });
});
