/**
 * Photos program (2026-08-27) — FormFillPage's GENERIC additional-photos pool adapter.
 *
 *   1. The adapter is supplied iff job + work date are set AND the role is pool-eligible
 *      (manager/admin — the Worker's DAILY_PHOTO_ROLES mirror; decision 9): the live
 *      uploader renders. A submitter gets the honest PLACEHOLDER instead (never an
 *      uploader whose every request would 403).
 *   2. Ref hygiene: pool refs are (job, date)-bound — changing either strips
 *      values.additional_photos (they would 4xx at submit as unknown_photo_ref).
 *   3. Load-&-amend changes NEITHER job nor date, so it must NOT strip the loaded refs —
 *      and it threads amendsUuid into the adapter (the list read's `amends=` param).
 *
 * Harness mirrors FormFillPage.r3.test.tsx (mocked auth + api); the pool network client is
 * mocked like additional-photos-section.test.tsx. The daily report is the one bundled form
 * carrying a pool mount today, reached via deep-link prefill (the picker hides it, prefill
 * does not).
 */
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

let mockRole = "manager";
vi.mock("../../lib/auth", () => ({
  useAuth: () => ({
    user: { username: "mgr.test", role: mockRole },
    loading: false,
    login: vi.fn(),
    logout: vi.fn(),
  }),
}));

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return {
    ...actual,
    fetchJobs: vi.fn(),
    fetchRecent: vi.fn(),
    submitForm: vi.fn(),
    listAccounts: vi.fn().mockResolvedValue([]),
  };
});

vi.mock("../../lib/fieldops_daily_photos", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/fieldops_daily_photos")>();
  return {
    ...actual,
    uploadDailyPhoto: vi.fn(),
    deleteDailyPhoto: vi.fn(),
    listDailyPhotos: vi.fn(),
  };
});

import * as api from "../../lib/api";
import { listDailyPhotos } from "../../lib/fieldops_daily_photos";
import { FormFillPage } from "../FormFillPage";

afterEach(cleanup);
beforeEach(() => {
  mockRole = "manager";
  vi.clearAllMocks();
  vi.mocked(api.fetchJobs).mockResolvedValue([{ job_id: "J1", project_name: "North Ridge" } as api.Job]);
  vi.mocked(api.fetchRecent).mockResolvedValue(null);
  vi.mocked(api.submitForm).mockResolvedValue(undefined as never);
  vi.mocked(listDailyPhotos).mockResolvedValue([]);
});

// The daily report deep-link (the one bundled pool-mount form; picker-hidden, prefill-reachable).
const DAILY_LINK = { jobId: "J1", parentCode: "daily-report", workDate: "2026-07-01" };

async function settle(container: HTMLElement) {
  await waitFor(() => expect(container.querySelector('option[value="J1"]')).not.toBeNull());
}

describe("FormFillPage — pool adapter presence (decision 9: manager/admin only)", () => {
  it("manager + job + date → the LIVE uploader (adapter supplied), not the placeholder", async () => {
    const { container, getByText, queryByText } = render(
      <FormFillPage onBack={() => {}} prefill={DAILY_LINK} />,
    );
    await settle(container as HTMLElement);
    expect(getByText("+ Add more photos")).toBeTruthy();
    expect(queryByText(/once a job and work\s+date are selected/)).toBeNull();
  });

  it("submitter with job + date → the PLACEHOLDER (no adapter, no uploader)", async () => {
    mockRole = "submitter";
    const { container, getByText, queryByText } = render(
      <FormFillPage onBack={() => {}} prefill={DAILY_LINK} />,
    );
    await settle(container as HTMLElement);
    expect(queryByText("+ Add more photos")).toBeNull();
    expect(getByText(/crew-lead manager or admin account/)).toBeTruthy();
    expect(listDailyPhotos).not.toHaveBeenCalled(); // the placeholder performs no network
  });

  it("manager with NO job selected → the placeholder until a job is chosen", async () => {
    const { container, getByText, queryByText } = render(
      <FormFillPage
        onBack={() => {}}
        prefill={{ parentCode: "daily-report", workDate: "2026-07-01" }}
      />,
    );
    await settle(container as HTMLElement);
    expect(queryByText("+ Add more photos")).toBeNull();
    expect(getByText(/once a job and work\s+date are selected/)).toBeTruthy();

    // Choosing the job completes the scope — the adapter appears.
    const jobSelect = container.querySelector("select") as HTMLSelectElement;
    fireEvent.change(jobSelect, { target: { value: "J1" } });
    await waitFor(() => expect(getByText("+ Add more photos")).toBeTruthy());
  });
});

describe("FormFillPage — pool-ref hygiene on scope change", () => {
  const SEEDED = { additional_photos: [{ pool_id: 5, caption: "seeded ref" }] };

  it("prefill-seeded refs SURVIVE the first render (no strip on mount)", async () => {
    const { container, findByLabelText } = render(
      <FormFillPage onBack={() => {}} prefill={{ ...DAILY_LINK, values: SEEDED }} />,
    );
    await settle(container as HTMLElement);
    expect(((await findByLabelText("Caption (photo 1)")) as HTMLInputElement).value).toBe("seeded ref");
  });

  it("changing the WORK DATE strips the refs (they are (job, date)-bound)", async () => {
    const { container, findByLabelText, queryByLabelText } = render(
      <FormFillPage onBack={() => {}} prefill={{ ...DAILY_LINK, values: SEEDED }} />,
    );
    await settle(container as HTMLElement);
    await findByLabelText("Caption (photo 1)");

    const dateInput = container.querySelector('input[type="date"]') as HTMLInputElement;
    fireEvent.change(dateInput, { target: { value: "2026-07-02" } });
    await waitFor(() => expect(queryByLabelText("Caption (photo 1)")).toBeNull());
  });

  it("changing the JOB strips the refs too", async () => {
    vi.mocked(api.fetchJobs).mockResolvedValue([
      { job_id: "J1", project_name: "North Ridge" } as api.Job,
      { job_id: "J2", project_name: "South Mesa" } as api.Job,
    ]);
    const { container, findByLabelText, queryByLabelText } = render(
      <FormFillPage onBack={() => {}} prefill={{ ...DAILY_LINK, values: SEEDED }} />,
    );
    await settle(container as HTMLElement);
    await findByLabelText("Caption (photo 1)");

    const jobSelect = container.querySelector("select") as HTMLSelectElement;
    fireEvent.change(jobSelect, { target: { value: "J2" } });
    await waitFor(() => expect(queryByLabelText("Caption (photo 1)")).toBeNull());
  });
});

describe("FormFillPage — load-&-amend keeps refs and threads amendsUuid", () => {
  it("amend load does NOT strip (job/date unchanged) and the amends uuid reaches the list read", async () => {
    vi.mocked(api.fetchRecent).mockResolvedValue({
      submission_uuid: "uuid-old",
      values: { additional_photos: [{ pool_id: 9, caption: "kept" }] },
    });
    const { container, getByText, findByLabelText } = render(
      <FormFillPage onBack={() => {}} prefill={DAILY_LINK} />,
    );
    await settle(container as HTMLElement);
    await waitFor(() => expect(getByText("Load & amend it")).toBeTruthy());
    fireEvent.click(getByText("Load & amend it"));

    // The loaded refs survive — loadAmend changes neither jobId nor workDate, so the
    // hygiene effect must not fire on it.
    expect(((await findByLabelText("Caption (photo 1)")) as HTMLInputElement).value).toBe("kept");
    // amendsUuid threads through the adapter into the pool list read (`amends=`), so the
    // amended report's own claimed rows chip "on file" instead of "missing".
    await waitFor(() =>
      expect(listDailyPhotos).toHaveBeenLastCalledWith("J1", "2026-07-01", "uuid-old"),
    );
  });
});
