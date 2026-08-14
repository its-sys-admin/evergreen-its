/**
 * WeeklyPhotoUpload (0074, Track B-C) — the office's week-scoped pool uploader.
 *
 * The contract under test:
 *   1. uploads carry origin:'office_wpr' and the SELECTED day's work_date;
 *   2. the day select spans the Sat→Fri week and defaults inside it;
 *   3. a pending upload chips "Screening…", flips on the poll, and fires onPoolChanged
 *      exactly when a row turns clean (the page then re-fetches its offered list);
 *   4. pool_cap_reached / pool_backlogged map to actionable copy, not a raw code.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/fieldops_daily_photos", () => ({
  uploadDailyPhoto: vi.fn(),
  listDailyPhotos: vi.fn(),
}));
vi.mock("../PhotoField", () => ({
  encodePhoto: vi.fn(async () => ({ data: "AAAA", name: "x.jpg", taken_at: "", gps: "" })),
}));

import { uploadDailyPhoto, listDailyPhotos } from "../../lib/fieldops_daily_photos";
import { ApiError } from "../../lib/errorCopy";
import { WeeklyPhotoUpload } from "../WeeklyPhotoUpload";

const WEEK = "2026-08-08"; // a Saturday; week runs to Fri 2026-08-14

function pickFile(): void {
  const input = screen.getByLabelText("Add photos to this week's pool") as HTMLInputElement;
  const file = new File(["x"], "x.jpg", { type: "image/jpeg" });
  fireEvent.change(input, { target: { files: [file] } });
}

beforeEach(() => {
  vi.resetAllMocks();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("WeeklyPhotoUpload", () => {
  it("offers the week's seven days and uploads to the selected one with origin office_wpr", async () => {
    vi.mocked(uploadDailyPhoto).mockResolvedValue({ ok: true, pool_id: 7, status: "pending" });
    const onPoolChanged = vi.fn();
    render(<WeeklyPhotoUpload jobId="JOB-1" weekStart={WEEK} onPoolChanged={onPoolChanged} />);

    const select = screen.getByLabelText("Day the added photos were taken") as HTMLSelectElement;
    expect(select.options).toHaveLength(7);
    expect(select.options[0].value).toBe("2026-08-08");
    expect(select.options[6].value).toBe("2026-08-14");

    fireEvent.change(select, { target: { value: "2026-08-11" } });
    pickFile();

    await waitFor(() => expect(uploadDailyPhoto).toHaveBeenCalledOnce());
    expect(vi.mocked(uploadDailyPhoto).mock.calls[0][1]).toBe("2026-08-11");
    expect(vi.mocked(uploadDailyPhoto).mock.calls[0][3]).toEqual({ origin: "office_wpr" });
    expect(await screen.findByText(/Screening…/)).toBeTruthy();
    expect(onPoolChanged).not.toHaveBeenCalled(); // nothing clean yet
  });

  it("flips the chip and fires onPoolChanged when the poll sees the row turn clean", async () => {
    vi.useFakeTimers();
    vi.mocked(uploadDailyPhoto).mockResolvedValue({ ok: true, pool_id: 7, status: "pending" });
    vi.mocked(listDailyPhotos).mockResolvedValue([
      { id: 7, status: "clean", created_at: 1, screened_at: 2, claimed: 0 },
    ]);
    const onPoolChanged = vi.fn();
    render(<WeeklyPhotoUpload jobId="JOB-1" weekStart={WEEK} onPoolChanged={onPoolChanged} />);

    pickFile();
    await vi.waitFor(() => expect(uploadDailyPhoto).toHaveBeenCalledOnce());

    await vi.advanceTimersByTimeAsync(15_500);
    await vi.waitFor(() => expect(onPoolChanged).toHaveBeenCalledOnce());
    await vi.waitFor(() => expect(screen.getByText(/In the pool/)).toBeTruthy());
  });

  it("maps pool_cap_reached to actionable copy", async () => {
    vi.mocked(uploadDailyPhoto).mockRejectedValue(new ApiError("pool_cap_reached", 409));
    render(<WeeklyPhotoUpload jobId="JOB-1" weekStart={WEEK} onPoolChanged={() => {}} />);
    pickFile();
    expect(await screen.findByRole("alert")).toHaveProperty(
      "textContent",
      expect.stringContaining("pick another day"),
    );
  });
});
