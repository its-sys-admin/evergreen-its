/**
 * additional_photos section + the daily-report-v6 cut (DR-photo-pool Slice 1 — the two
 * 2026-07-03 operator directives in ONE version cut).
 *
 * Directive 1 (more photos): the inline 4-photo site_photos field STAYS (payload-budgeted —
 * CS2: 280KB × 4 base64 < the Worker's 1.8MB cap); below it the additional_photos MOUNT renders
 * the pool uploader — each extra photo uploads INDIVIDUALLY (uploadDailyPhoto) and the form
 * values carry only tiny references ([{pool_id, caption?}]), so drafts persist refs with zero
 * sessionStorage quota pressure. Directive 2 (incident link): a form_link → material-incident
 * sits under the D.13 Deliveries Received table.
 *
 * Contract asserted here:
 *   • v6 structure: the mount right after the Site photos header; the incident link right after
 *     deliveries_received; both directives in one definition; v6 keeps the D4/M2 floor mounts;
 *   • no adapter → the section renders NOTHING (generic fill page / other forms inert) and
 *     contributes NO initialValues key;
 *   • adapter → upload appends a ref (caption defaults to the file name) with the G1 chip
 *     vocabulary (pending "Screening…"); remove deletes live rows from the pool but drops
 *     refused/missing REFS without a delete call (forensic markers stay);
 *   • statuses reconcile from listDailyPhotos (clean / refused / missing chips);
 *   • the D.13 link renders + deep-links through the EXISTING FormLinkAdapter machinery with
 *     the live material-incident filed indicator.
 */
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/fieldops_daily_photos", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/fieldops_daily_photos")>();
  return {
    ...actual,
    uploadDailyPhoto: vi.fn(),
    deleteDailyPhoto: vi.fn(),
    listDailyPhotos: vi.fn(),
  };
});
vi.mock("../../components/PhotoField", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../components/PhotoField")>();
  // encodePhoto needs a real canvas (jsdom has none) — the encoder itself is covered by
  // photo-field.test.tsx; here it's a fixture source.
  return { ...actual, encodePhoto: vi.fn() };
});

import { AdditionalPhotosSection } from "../../components/AdditionalPhotosSection";
import { encodePhoto } from "../../components/PhotoField";
import {
  deleteDailyPhoto,
  listDailyPhotos,
  uploadDailyPhoto,
  type DailyPoolPhotoRow,
} from "../../lib/fieldops_daily_photos";
import { FormRenderer, initialValues, type FormLinkAdapter } from "../FormRenderer";
import { formCatalog, getDefinition } from "../registry";
import type { FormDefinition, PhotoValue } from "../types";

afterEach(cleanup);
beforeEach(() => {
  vi.mocked(listDailyPhotos).mockResolvedValue([]);
  vi.mocked(uploadDailyPhoto).mockReset();
  vi.mocked(deleteDailyPhoto).mockReset();
  vi.mocked(encodePhoto).mockReset();
});

// Resolve the CURRENT daily report from the catalog rather than naming a version. The eager
// registry window is "current + immediately-previous" (vite-plugin-eager-forms), so a
// hard-coded code silently becomes null two cuts later — which is exactly what v7 did to the
// v5 references here. These tests are about renderer behaviour, not a historical version.
const DEF = getDefinition(
  formCatalog().find((p) => p.parent_form_code === "daily-report")!.form_code!,
) as FormDefinition;

function poolRow(over: Partial<DailyPoolPhotoRow> & { id: number }): DailyPoolPhotoRow {
  return { status: "pending", created_at: 1_700_000_000, screened_at: null, claimed: 0, ...over };
}

describe("daily-report-v6 — ONE cut carrying BOTH 2026-07-03 directives", () => {
  it("mounts ONE additional_photos section immediately below the Site photos field (directive 1)", () => {
    expect(DEF).not.toBeNull();
    const mounts = DEF.sections.filter((s) => s.type === "additional_photos");
    expect(mounts).toHaveLength(1);
    expect(mounts[0]).toMatchObject({ key: "additional_photos", title: "Additional site photos" });
    const idx = DEF.sections.findIndex((s) => s.type === "additional_photos");
    // The 4-photo inline field STAYS untouched, directly above the mount.
    expect(DEF.sections[idx - 1]).toMatchObject({ type: "header", title: "Site photos" });
    const header = DEF.sections[idx - 1] as Extract<FormDefinition["sections"][number], { type: "header" }>;
    expect(header.fields).toEqual([{ key: "site_photos", label: "Site photos", input: "photo" }]);
  });

  it("links the material-incident form directly under the D.13 Deliveries Received table (directive 2)", () => {
    const idx = DEF.sections.findIndex(
      (s) => s.type === "form_link" && s.parent_form_code === "material-incident",
    );
    expect(idx).toBeGreaterThan(0);
    expect(DEF.sections[idx]).toMatchObject({ type: "form_link", label: "Report a material incident" });
    expect(DEF.sections[idx - 1]).toMatchObject({ type: "repeating_table", key: "deliveries_received" });
    expect(DEF.sections[idx + 1]).toMatchObject({ type: "repeating_table", key: "equipment_on_site" });
  });

  it("keeps the required_section_types floor mounts (job_requirements + expected_materials)", () => {
    const types = DEF.sections.map((s) => s.type);
    expect(types).toContain("job_requirements");
    expect(types).toContain("expected_materials");
  });

  it("contributes NO initialValues key (refs land only when photos are added)", () => {
    expect("additional_photos" in initialValues(DEF)).toBe(false);
  });
});

describe("additional_photos mount — inert without the adapter", () => {
  it("renders NOTHING on the generic fill page (no adapter) and never fetches", () => {
    const { container } = render(
      <FormRenderer def={DEF} values={initialValues(DEF)} setValues={() => {}} />,
    );
    expect(container.querySelector(".fr__additional-photos")).toBeNull();
    expect(listDailyPhotos).not.toHaveBeenCalled();
  });

  it("renders the pool uploader when the host supplies the scope adapter", () => {
    const { container, getByText } = render(
      <FormRenderer
        def={DEF}
        values={initialValues(DEF)}
        setValues={() => {}}
        additionalPhotos={{ jobId: "JOB-A", workDate: "2026-07-03" }}
      />,
    );
    expect(container.querySelector(".fr__additional-photos")).not.toBeNull();
    expect(getByText("Additional site photos")).toBeTruthy();
    expect(getByText("+ Add more photos")).toBeTruthy();
  });
});

describe("AdditionalPhotosSection — upload appends pool REFS (never bytes)", () => {
  const PHOTO: PhotoValue = { data: "abc", name: "trench.jpg", taken_at: "", gps: "" };

  it("encodes → uploads to the (job, date) pool → appends {pool_id, caption=file name} with a pending chip", async () => {
    vi.mocked(encodePhoto).mockResolvedValue(PHOTO);
    vi.mocked(uploadDailyPhoto).mockResolvedValue({ ok: true, pool_id: 101, status: "pending" });
    const onChange = vi.fn();
    const { getByTestId } = render(
      <AdditionalPhotosSection jobId="JOB-A" workDate="2026-07-03" refs={[]} onChange={onChange} />,
    );
    fireEvent.change(getByTestId("additional-photos-input"), {
      target: { files: [new File(["x"], "trench.jpg", { type: "image/jpeg" })] },
    });
    await waitFor(() => expect(onChange).toHaveBeenCalledWith([{ pool_id: 101, caption: "trench.jpg" }]));
    expect(uploadDailyPhoto).toHaveBeenCalledWith("JOB-A", "2026-07-03", PHOTO);
  });

  it("an upload failure surfaces actionable copy and appends nothing", async () => {
    vi.mocked(encodePhoto).mockResolvedValue(PHOTO);
    vi.mocked(uploadDailyPhoto).mockRejectedValue(new Error("That photo is too large to upload — retake it at a lower quality."));
    const onChange = vi.fn();
    const { getByTestId, findByRole } = render(
      <AdditionalPhotosSection jobId="JOB-A" workDate="2026-07-03" refs={[]} onChange={onChange} />,
    );
    fireEvent.change(getByTestId("additional-photos-input"), {
      target: { files: [new File(["x"], "big.jpg", { type: "image/jpeg" })] },
    });
    const alert = await findByRole("alert");
    expect(alert.textContent).toContain("too large");
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe("AdditionalPhotosSection — draft refs, chips and removal", () => {
  it("reconciles restored draft refs against the pool: pending/clean/refused chips + missing copy", async () => {
    vi.mocked(listDailyPhotos).mockResolvedValue([
      poolRow({ id: 1, status: "pending" }),
      poolRow({ id: 2, status: "clean", screened_at: 1_700_000_100 }),
      poolRow({ id: 3, status: "refused" }),
    ]);
    const { findByText, getByText } = render(
      <AdditionalPhotosSection
        jobId="JOB-A"
        workDate="2026-07-03"
        refs={[{ pool_id: 1 }, { pool_id: 2 }, { pool_id: 3 }, { pool_id: 4 }]}
        onChange={vi.fn()}
      />,
    );
    expect(await findByText("Screening…")).toBeTruthy();
    expect(getByText("Photo on file ✓")).toBeTruthy();
    expect(getByText(/Refused — remove it/)).toBeTruthy();
    expect(getByText(/No longer available/)).toBeTruthy(); // pool row 4 vanished (pruned/claimed)
    expect(getByText(/\(4\/40\)/)).toBeTruthy(); // the visible cap counter
  });

  it("removing a LIVE ref deletes the pool row first; removing a REFUSED ref only drops the ref", async () => {
    vi.mocked(listDailyPhotos).mockResolvedValue([
      poolRow({ id: 1, status: "pending" }),
      poolRow({ id: 2, status: "refused" }),
    ]);
    vi.mocked(deleteDailyPhoto).mockResolvedValue(undefined);
    const onChange = vi.fn();
    const { findByText, getByLabelText } = render(
      <AdditionalPhotosSection
        jobId="JOB-A"
        workDate="2026-07-03"
        refs={[{ pool_id: 1, caption: "one" }, { pool_id: 2, caption: "two" }]}
        onChange={onChange}
      />,
    );
    await findByText("Screening…"); // statuses landed

    fireEvent.click(getByLabelText("Remove additional photo 1"));
    await waitFor(() =>
      expect(onChange).toHaveBeenCalledWith([{ pool_id: 2, caption: "two" }]),
    );
    expect(deleteDailyPhoto).toHaveBeenCalledWith(1);

    vi.mocked(deleteDailyPhoto).mockClear();
    fireEvent.click(getByLabelText("Remove additional photo 2"));
    await waitFor(() => expect(onChange).toHaveBeenCalledWith([{ pool_id: 1, caption: "one" }]));
    expect(deleteDailyPhoto).not.toHaveBeenCalled(); // refused = byte-free forensic marker, ref-drop only
  });

  it("caption edits rewrite the ref (the tiny draft-persisted payload)", async () => {
    const onChange = vi.fn();
    const { findByLabelText } = render(
      <AdditionalPhotosSection
        jobId="JOB-A"
        workDate="2026-07-03"
        refs={[{ pool_id: 7, caption: "old" }]}
        onChange={onChange}
      />,
    );
    fireEvent.change(await findByLabelText("Caption (photo 1)"), { target: { value: "new caption" } });
    expect(onChange).toHaveBeenCalledWith([{ pool_id: 7, caption: "new caption" }]);
  });

  it("a failed status read soft-warns with a Retry — never a blocker (the R2 never-silent bar)", async () => {
    vi.mocked(listDailyPhotos).mockRejectedValue(new Error("network down"));
    const { findByText, getByText } = render(
      <AdditionalPhotosSection jobId="JOB-A" workDate="2026-07-03" refs={[{ pool_id: 1 }]} onChange={vi.fn()} />,
    );
    expect(await findByText(/Couldn't check photo screening status/)).toBeTruthy();
    expect(getByText("Retry")).toBeTruthy();
    expect(getByText("+ Add more photos")).toBeTruthy(); // still usable
  });
});

describe("AdditionalPhotosSection — amend mode (the filed report's own claimed rows)", () => {
  it("threads amendsUuid into the list read and chips claimed rows 'Photo on file ✓' — never 'missing'", async () => {
    // The Worker's amend read: rows claimed by the verified amends target ride with claimed:1
    // (a claimed-but-unscreened row is on-file too — the filed report owns it either way).
    vi.mocked(listDailyPhotos).mockResolvedValue([
      poolRow({ id: 1, status: "clean", claimed: 1, screened_at: 1_700_000_100 }),
      poolRow({ id: 2, status: "pending", claimed: 1 }),
    ]);
    const { findAllByText, queryByText } = render(
      <AdditionalPhotosSection
        jobId="JOB-A"
        workDate="2026-07-03"
        amendsUuid="prior-1"
        refs={[{ pool_id: 1, caption: "one" }, { pool_id: 2, caption: "two" }]}
        onChange={vi.fn()}
      />,
    );
    expect(await findAllByText("Photo on file ✓")).toHaveLength(2);
    // The pre-fix bug: the unclaimed-only read resolved these refs "missing" and told the
    // amending manager to remove perfectly-valid filed photos.
    expect(queryByText(/No longer available/)).toBeNull();
    expect(queryByText("Screening…")).toBeNull();
    expect(listDailyPhotos).toHaveBeenCalledWith("JOB-A", "2026-07-03", "prior-1");
  });

  it("removing an on-file ref drops the REF only — never a pool delete (the claim is the FILED report's linkage)", async () => {
    vi.mocked(listDailyPhotos).mockResolvedValue([poolRow({ id: 1, status: "clean", claimed: 1 })]);
    const onChange = vi.fn();
    const { findByText, getByLabelText } = render(
      <AdditionalPhotosSection
        jobId="JOB-A"
        workDate="2026-07-03"
        amendsUuid="prior-1"
        refs={[{ pool_id: 1, caption: "one" }]}
        onChange={onChange}
      />,
    );
    await findByText("Photo on file ✓");
    fireEvent.click(getByLabelText("Remove additional photo 1"));
    await waitFor(() => expect(onChange).toHaveBeenCalledWith([]));
    expect(deleteDailyPhoto).not.toHaveBeenCalled(); // the Worker would 409 photo_claimed anyway
  });
});

describe("the D.13 material-incident link renders through the existing form_link machinery", () => {
  it("renders the button + filed indicator and deep-links via FormLinkAdapter.open", () => {
    const open = vi.fn();
    const formLinks: FormLinkAdapter = {
      open,
      filedLabel: (code) => (code === "material-incident" ? "Filed ✓ 2:14 PM by Mo Manager" : null),
    };
    const { getByText } = render(
      <FormRenderer def={DEF} values={initialValues(DEF)} setValues={() => {}} formLinks={formLinks} />,
    );
    const btn = getByText(/Report a material incident/);
    expect(btn).toBeTruthy();
    fireEvent.click(btn);
    expect(open).toHaveBeenCalledWith("material-incident");
    expect(getByText("Filed ✓ 2:14 PM by Mo Manager")).toBeTruthy();
  });
});
