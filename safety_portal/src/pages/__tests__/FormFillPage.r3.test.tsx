/**
 * R3 — FormFillPage deep-link polish + returnTo round trip + dirty guard.
 *
 *   1. Submitted-screen variants: deep-link (returnTo present → primary "Back to My Tasks" +
 *      "checks off your checklist item" line) vs normal (unchanged Submit another / Home).
 *   2. Deep-link polish: the heading names the prefilled form; the prefilled job renders as
 *      read-only text while jobs load (never a blank select).
 *   3. Dirty guard: touching a form field reports dirty up (onDirtyChange) + arms beforeunload;
 *      submit clears it. Pre-submit "← Back …" confirms before discarding a dirty form.
 *
 * Harness mirrors FormFillPage.pdf.test.tsx (mocked auth + api, no jest-dom).
 */
import { act, cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/auth", () => ({
  useAuth: () => ({
    user: { username: "pm.test", role: "submitter" },
    loading: false,
    login: vi.fn(),
    logout: vi.fn(),
  }),
}));

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return {
    ...actual,
    fetchJobs: vi.fn().mockResolvedValue([{ job_id: "J1", project_name: "North Ridge" }]),
    fetchRecent: vi.fn().mockResolvedValue(null),
    submitForm: vi.fn().mockResolvedValue(undefined),
    requestPdf: vi.fn().mockResolvedValue({ ok: true, ready: false }),
    pdfStatus: vi.fn().mockResolvedValue({ requested: true, ready: true, expires_at: 1_900_000_000 }),
    downloadPdf: vi.fn(),
  };
});

import * as api from "../../lib/api";
import { FormFillPage } from "../FormFillPage";

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.fetchJobs).mockResolvedValue([{ job_id: "J1", project_name: "North Ridge" } as never]);
  vi.mocked(api.fetchRecent).mockResolvedValue(null);
  vi.mocked(api.submitForm).mockResolvedValue(undefined as never);
});

const DEEP_LINK = { jobId: "J1", parentCode: "jha", workDate: "2026-07-01" };

async function submitPrefilled(container: HTMLElement, getByText: (t: string) => HTMLElement) {
  // The prefill already carries job + form; wait for jobs, then submit.
  await waitFor(() => expect(container.querySelector('option[value="J1"]')).not.toBeNull());
  await waitFor(() => expect(getByText("Submit")).toBeTruthy());
  fireEvent.click(getByText("Submit"));
  await waitFor(() => expect(getByText("Submitted ✓")).toBeTruthy());
}

describe("FormFillPage — R3 deep-link polish", () => {
  it("names the prefilled form in the heading (not 'New safety form')", async () => {
    const { container } = render(<FormFillPage onBack={() => {}} prefill={DEEP_LINK} />);
    const heading = container.querySelector("h1.page__heading");
    expect(heading?.textContent).toBe("Job Hazard Analysis");
    await waitFor(() => expect(container.querySelector('option[value="J1"]')).not.toBeNull());
  });

  it("keeps the generic heading for a non-deep-link fill", async () => {
    const { container } = render(<FormFillPage onBack={() => {}} />);
    expect(container.querySelector("h1.page__heading")?.textContent).toBe("New safety form");
    await waitFor(() => expect(container.querySelector('option[value="J1"]')).not.toBeNull());
  });

  it("shows the prefilled job as read-only text while jobs load, then the real select", async () => {
    let resolveJobs: (jobs: api.Job[]) => void = () => {};
    vi.mocked(api.fetchJobs).mockReturnValue(new Promise((res) => { resolveJobs = res; }));
    const { container, getByLabelText } = render(<FormFillPage onBack={() => {}} prefill={DEEP_LINK} />);

    // While loading: no blank Job select — a read-only row naming the deep-linked job.
    expect(container.querySelector('option[value="J1"]')).toBeNull();
    expect(getByLabelText("Job (from your checklist)").textContent).toBe("J1");

    await act(async () => {
      resolveJobs([{ job_id: "J1", project_name: "North Ridge" } as api.Job]);
    });
    await waitFor(() => expect(container.querySelector('option[value="J1"]')).not.toBeNull());
    const jobSelect = container.querySelector("select") as HTMLSelectElement;
    expect(jobSelect.value).toBe("J1");
  });
});

describe("FormFillPage — R3 submitted-screen variants", () => {
  it("deep-link: primary 'Back to My Tasks' + the checks-off line; return fires the callback", async () => {
    const onReturn = vi.fn();
    const { container, getByText, queryByText } = render(
      <FormFillPage
        onBack={() => {}}
        prefill={DEEP_LINK}
        returnTo={{ label: "Back to My Tasks", onReturn }}
      />,
    );
    await submitPrefilled(container as HTMLElement, getByText);

    expect(container.textContent ?? "").toContain("checks off your checklist item");
    const back = getByText("Back to My Tasks");
    expect(back.className).toContain("btn--primary");
    expect(getByText("Submit another").className).toContain("btn--secondary");
    expect(queryByText("Home")).toBeNull(); // the deep-link variant returns to the origin, not Home

    fireEvent.click(back);
    expect(onReturn).toHaveBeenCalledTimes(1);
  });

  it("normal fill: default actions unchanged (Submit another primary + Home), no checklist copy", async () => {
    const { container, getByText, queryByText } = render(
      <FormFillPage onBack={() => {}} prefill={DEEP_LINK} />,
    );
    await submitPrefilled(container as HTMLElement, getByText);

    expect(getByText("Submit another").className).toContain("btn--primary");
    expect(getByText("Home")).toBeTruthy();
    expect(queryByText("Back to My Tasks")).toBeNull();
    expect(container.textContent ?? "").not.toContain("checks off your checklist item");
  });
});

describe("FormFillPage — R3 dirty guard", () => {
  // Find the first FormRenderer text input (the select card only holds selects + a date input).
  function firstFormInput(container: HTMLElement): HTMLInputElement {
    const input = Array.from(container.querySelectorAll("input")).find((i) => i.type === "text");
    expect(input).toBeTruthy();
    return input as HTMLInputElement;
  }

  it("touching a field reports dirty + arms beforeunload; submit clears both", async () => {
    const onDirtyChange = vi.fn();
    const { container, getByText } = render(
      <FormFillPage onBack={() => {}} prefill={DEEP_LINK} onDirtyChange={onDirtyChange} />,
    );
    await waitFor(() => expect(getByText("Submit")).toBeTruthy());
    // await the last-call value — the mount's onDirtyChange(false) can settle a tick after the
    // Submit button renders, so a bare assertion here is timing-sensitive (flaky).
    await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(false)); // mount reports clean

    fireEvent.change(firstFormInput(container as HTMLElement), { target: { value: "typed something" } });
    await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(true));

    const ev1 = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(ev1);
    expect(ev1.defaultPrevented).toBe(true);

    fireEvent.click(getByText("Submit"));
    await waitFor(() => expect(getByText("Submitted ✓")).toBeTruthy());
    // submit clears dirty — again a tick behind the "Submitted ✓" render, so await the last call.
    await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(false));

    const ev2 = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(ev2);
    expect(ev2.defaultPrevented).toBe(false);
  });

  it("pre-submit '← Back …' returns straight through when clean, confirms when dirty", async () => {
    const onReturn = vi.fn();
    const confirmSpy = vi.spyOn(window, "confirm");
    const { container, getByText } = render(
      <FormFillPage onBack={() => {}} prefill={DEEP_LINK} returnTo={{ label: "Back to My Tasks", onReturn }} />,
    );
    await waitFor(() => expect(getByText("Submit")).toBeTruthy());

    // Visible on the light page ground. This shipped as the white-on-green HEADER variant,
    // rendering white-on-#f7f6f2 at ~1.04:1 — the operator reported it as invisible.
    // --secondary is the canonical in-page back control, and it matches the post-submit twin
    // above, which was already green. (The complementary "and NOT the header variant" half is
    // asserted repo-wide by tests/test_portal_button_variants.py; spelling it out here would
    // put that very class name in a source file the scanner reads, tripping its own gate.)
    expect(getByText("← Back to My Tasks").className).toContain("btn--secondary");

    // Clean → no confirm, straight back.
    fireEvent.click(getByText("← Back to My Tasks"));
    expect(confirmSpy).not.toHaveBeenCalled();
    expect(onReturn).toHaveBeenCalledTimes(1);

    // Dirty → confirm gates the discard.
    fireEvent.change(firstFormInput(container as HTMLElement), { target: { value: "unsaved" } });
    confirmSpy.mockReturnValueOnce(false);
    fireEvent.click(getByText("← Back to My Tasks"));
    expect(onReturn).toHaveBeenCalledTimes(1); // declined — stayed

    confirmSpy.mockReturnValueOnce(true);
    fireEvent.click(getByText("← Back to My Tasks"));
    expect(onReturn).toHaveBeenCalledTimes(2);
    confirmSpy.mockRestore();
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// D2 (SOP daily form) — the Submit-a-Form CREATE picker hides launch:"daily-tab" parents.
// The Daily Report is filed from My Tasks → Daily report; the office's retrieval surfaces
// (Form Request / download / history) are untouched, and the definition itself stays resolvable
// (the FormFillPage.pdf.test.tsx prefill spec proves a daily-report deep-link still renders).
// ─────────────────────────────────────────────────────────────────────────────
describe("FormFillPage — D2 CREATE picker hides daily-tab parents", () => {
  it("offers the other parents but NOT the Daily Report (launch:'daily-tab')", async () => {
    const { container } = render(<FormFillPage onBack={() => {}} />);
    await waitFor(() => expect(container.querySelector('option[value="J1"]')).not.toBeNull());
    const formSelect = Array.from(container.querySelectorAll("select"))[1];
    const options = Array.from(formSelect.querySelectorAll("option")).map((o) => o.value);
    expect(options).toContain("jha");
    expect(options).toContain("incident-report");
    expect(options).not.toContain("daily-report");
    expect(formSelect.textContent ?? "").not.toContain("Daily Field Report");
  });
});
