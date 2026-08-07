/**
 * R3 — ChecklistItemRow interaction tests: count freeze/undo + the row-owned below-target
 * acknowledge flow (server-driven via ApiError 'below_target'), manual_attest edit-note +
 * photo-evidence rendering, and the form_linked dead-end explanations / Filed ✓ state.
 *
 * PROP-COMPAT: the row is exercised BOTH ways — with the pre-R3 prop shape (legacy path;
 * parallel-R2's callers) and with the R3 opt-in `onCountRecorded` (row-owned count flow).
 */
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../lib/fieldops_checklist", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/fieldops_checklist")>();
  return { ...actual, recordCountItem: vi.fn(), uploadItemPhoto: vi.fn() };
});
// G1: encodePhoto is PhotoField's downscale ladder (jsdom has no canvas/createImageBitmap) —
// mocked to a deterministic PhotoValue so the row's capture flow is unit-testable.
vi.mock("../PhotoField", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../PhotoField")>();
  return { ...actual, encodePhoto: vi.fn() };
});

import * as checklist from "../../lib/fieldops_checklist";
import { encodePhoto } from "../PhotoField";
import { ApiError } from "../../lib/errorCopy";
import {
  ChecklistItemRow,
  DEADEND_FORM_UNAVAILABLE,
  DEADEND_NO_JOB_DATE,
} from "../ChecklistItemRow";

afterEach(cleanup);
beforeEach(() => vi.clearAllMocks());

function makeItem(over: Partial<checklist.ChecklistItemState> = {}): checklist.ChecklistItemState {
  return {
    id: 20,
    source_item_id: 5,
    item_type: "count",
    label: "Log deliveries",
    form_code: null,
    target_count: 3,
    status: "open",
    note: null,
    photo_ref: null,
    completed_by: null,
    completed_at: null,
    value_num: null,
    filed_by: null,
    photo_status: null,
    requires_photo: false,
    ...over,
  };
}

// Renders the row inside the <li> its parents own. Default props mirror the pre-R3 caller shape.
function renderRow(item: checklist.ChecklistItemState, over: Record<string, unknown> = {}) {
  const props = {
    item,
    busy: false,
    canOpenForm: true,
    onComplete: vi.fn(),
    onUncomplete: vi.fn(),
    onRecordCount: vi.fn(),
    onOpenForm: vi.fn(),
    ...over,
  };
  const utils = render(
    <ul>
      <li>
        <ChecklistItemRow {...(props as Parameters<typeof ChecklistItemRow>[0])} />
      </li>
    </ul>,
  );
  return { ...utils, props };
}

describe("ChecklistItemRow — requires_photo gate", () => {
  it("disables Mark done + shows the hint when a photo is required but none is attached", () => {
    const { getByLabelText, container } = renderRow(
      makeItem({ item_type: "manual_attest", label: "Snap it", requires_photo: true, photo_status: null }),
      { onPhotoUploaded: vi.fn() },
    );
    expect((getByLabelText("Complete item 20") as HTMLButtonElement).disabled).toBe(true);
    expect(container.textContent ?? "").toContain("Photo required");
  });

  it("enables Mark done once a pending OR clean photo is attached (hint gone)", () => {
    for (const photo_status of ["pending", "clean"] as const) {
      const { getByLabelText, container, unmount } = renderRow(
        makeItem({ item_type: "manual_attest", label: "Snap it", requires_photo: true, photo_status }),
        { onPhotoUploaded: vi.fn() },
      );
      expect((getByLabelText("Complete item 20") as HTMLButtonElement).disabled).toBe(false);
      expect(container.textContent ?? "").not.toContain("Photo required");
      unmount();
    }
  });

  it("a refused photo does NOT satisfy the gate", () => {
    const { getByLabelText } = renderRow(
      makeItem({ item_type: "manual_attest", requires_photo: true, photo_status: "refused" }),
      { onPhotoUploaded: vi.fn() },
    );
    expect((getByLabelText("Complete item 20") as HTMLButtonElement).disabled).toBe(true);
  });

  it("never gates an item without requires_photo", () => {
    const { getByLabelText } = renderRow(
      makeItem({ item_type: "manual_attest", requires_photo: false, photo_status: null }),
      { onPhotoUploaded: vi.fn() },
    );
    expect((getByLabelText("Complete item 20") as HTMLButtonElement).disabled).toBe(false);
  });
});

describe("ChecklistItemRow — count", () => {
  it("open: numeric keypad hint + the recorded value stays visible while open", () => {
    const { getByLabelText, container } = renderRow(makeItem({ value_num: 2 }));
    const input = getByLabelText("Count for item 20") as HTMLInputElement;
    expect(input.type).toBe("number");
    expect(input.getAttribute("inputmode")).toBe("numeric");
    expect(container.textContent ?? "").toContain("recorded 2");
    expect(getByLabelText("Record item 20")).not.toBeNull();
  });

  it("done: controls frozen (no input/Record), recorded value + Undo firing onUncomplete", () => {
    const item = makeItem({ status: "done", value_num: 5, completed_by: "mgr.mo" });
    const { queryByLabelText, getByLabelText, container, props } = renderRow(item);
    expect(queryByLabelText("Count for item 20")).toBeNull();
    expect(queryByLabelText("Record item 20")).toBeNull();
    expect(container.textContent ?? "").toContain("recorded 5");
    fireEvent.click(getByLabelText("Undo item 20"));
    expect(props.onUncomplete).toHaveBeenCalledWith(item);
  });

  it("an acknowledged below-target done item wears the 'below target' warn pill", () => {
    const { container } = renderRow(
      makeItem({ status: "done", value_num: 1, target_count: 3, note: "supplier shorted us" }),
    );
    const warn = container.querySelector(".dash-pill--warn");
    expect(warn?.textContent).toBe("below target");
    expect(container.textContent ?? "").toContain("supplier shorted us");
  });

  it("legacy path (no onCountRecorded): Record delegates to onRecordCount, no direct lib call", () => {
    const { getByLabelText, props } = renderRow(makeItem());
    fireEvent.change(getByLabelText("Count for item 20"), { target: { value: "5" } });
    fireEvent.click(getByLabelText("Record item 20"));
    expect(props.onRecordCount).toHaveBeenCalledWith(makeItem(), 5);
    expect(checklist.recordCountItem).not.toHaveBeenCalled();
  });

  it("row-owned: a met-target record calls the lib then onCountRecorded (never onRecordCount)", async () => {
    vi.mocked(checklist.recordCountItem).mockResolvedValue({
      ok: true, id: 20, status: "done", value_num: 5, instance_status: "open",
    });
    const onCountRecorded = vi.fn();
    const { getByLabelText, props } = renderRow(makeItem(), { onCountRecorded });
    fireEvent.change(getByLabelText("Count for item 20"), { target: { value: "5" } });
    fireEvent.click(getByLabelText("Record item 20"));
    await waitFor(() => expect(onCountRecorded).toHaveBeenCalledTimes(1));
    expect(checklist.recordCountItem).toHaveBeenCalledWith(20, 5);
    expect(props.onRecordCount).not.toHaveBeenCalled();
  });

  it("row-owned: server 'below_target' opens the inline prompt; 'Record anyway' needs a note and calls the R1 acknowledge path", async () => {
    vi.mocked(checklist.recordCountItem)
      .mockRejectedValueOnce(new ApiError("below_target", 400))
      .mockResolvedValueOnce({
        ok: true, id: 20, status: "done", value_num: 1, instance_status: "open", acknowledged_below_target: true,
      });
    const onCountRecorded = vi.fn();
    const { getByLabelText, container } = renderRow(makeItem(), { onCountRecorded });

    fireEvent.change(getByLabelText("Count for item 20"), { target: { value: "1" } });
    fireEvent.click(getByLabelText("Record item 20"));

    // The prompt appears, showing the recorded value + target; the confirm needs a note first.
    const anyway = await waitFor(() => getByLabelText("Record item 20 anyway") as HTMLButtonElement);
    expect(container.textContent ?? "").toContain("Recorded 1 of target 3");
    expect(anyway.disabled).toBe(true);
    expect(onCountRecorded).not.toHaveBeenCalled(); // the failed record must not trigger a refresh

    fireEvent.change(getByLabelText("Shortfall note for item 20"), { target: { value: "supplier shorted the load" } });
    expect((getByLabelText("Record item 20 anyway") as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(getByLabelText("Record item 20 anyway"));

    await waitFor(() => expect(onCountRecorded).toHaveBeenCalledTimes(1));
    expect(checklist.recordCountItem).toHaveBeenLastCalledWith(20, 1, {
      acknowledgeBelowTarget: true,
      note: "supplier shorted the load",
    });
  });

  it("row-owned: a non-below_target error surfaces inline (human copy, item stays interactive)", async () => {
    vi.mocked(checklist.recordCountItem).mockRejectedValue(new ApiError("invalid_value_num", 400));
    const onCountRecorded = vi.fn();
    const { getByLabelText, container } = renderRow(makeItem(), { onCountRecorded });
    fireEvent.change(getByLabelText("Count for item 20"), { target: { value: "2" } });
    fireEvent.click(getByLabelText("Record item 20"));
    await waitFor(() => expect(container.textContent ?? "").toContain("Enter a non-negative number."));
    expect(onCountRecorded).not.toHaveBeenCalled();
  });
});

describe("ChecklistItemRow — manual_attest", () => {
  it("open: completes with the typed note (labels unchanged for parallel-R2 callers)", () => {
    const item = makeItem({ id: 12, item_type: "manual_attest", label: "Record crew progress", target_count: null });
    const { getByLabelText, props } = renderRow(item);
    fireEvent.change(getByLabelText("Note for item 12"), { target: { value: "wet deck" } });
    fireEvent.click(getByLabelText("Complete item 12"));
    // G1: "Mark done" now threads the existing photo_ref (undefined here — none attached) so a
    // pending 'pending:<id>' stamp isn't clobbered by a plain completion.
    expect(props.onComplete).toHaveBeenCalledWith(item, "wet deck", undefined);
  });

  it("done: shows the note evidence, humanized subtitle, and Edit note re-completes threading photo_ref", () => {
    const item = makeItem({
      id: 12, item_type: "manual_attest", label: "Record crew progress", target_count: null,
      status: "done", note: "old note", photo_ref: "box:123", completed_by: "mgr.mo",
    });
    const { getByLabelText, container, props } = renderRow(item);
    const txt = container.textContent ?? "";
    expect(txt).toContain("old note");
    expect(txt).toContain("photo attached"); // non-renderable ref → marker, not a broken <img>
    expect(txt).toContain("· Check"); // itemTypeLabel — no raw manual_attest token
    expect(txt).not.toContain("manual_attest");

    fireEvent.click(getByLabelText("Edit note for item 12"));
    const editInput = getByLabelText("New note for item 12") as HTMLInputElement;
    expect(editInput.value).toBe("old note");
    fireEvent.change(editInput, { target: { value: "new note" } });
    fireEvent.click(getByLabelText("Save note for item 12"));
    // Idempotent re-complete: note updated, the existing photo_ref threaded through un-clobbered.
    expect(props.onComplete).toHaveBeenCalledWith(item, "new note", "box:123");
  });

  it("done: a data-URI photo_ref renders the neutral marker, NEVER an <img> (G1/Option D — the raw passthrough is retired)", () => {
    const ref = "data:image/jpeg;base64,abc123";
    const { container } = renderRow(
      makeItem({ id: 12, item_type: "manual_attest", target_count: null, status: "done", photo_ref: ref }),
    );
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain("photo attached");
  });
});

describe("ChecklistItemRow — G1 photo capture states (Option D: status-only, no image ever)", () => {
  const withPhoto = (over: Partial<checklist.ChecklistItemState> = {}, props: Record<string, unknown> = {}) =>
    renderRow(makeItem({ id: 30, item_type: "manual_attest", target_count: null, ...over }), {
      onPhotoUploaded: vi.fn(),
      ...props,
    });

  it("no photo UI at all when the parent doesn't opt in (byte-identical to pre-G1)", () => {
    const { container } = renderRow(makeItem({ id: 30, item_type: "manual_attest", target_count: null }));
    expect(container.querySelector('[data-testid="item-photo-input-30"]')).toBeNull();
    expect(container.textContent).not.toContain("Add photo");
  });

  it("none → an [Add photo] affordance (opted in)", () => {
    const { getByLabelText, container } = withPhoto({ photo_status: null });
    expect(getByLabelText("Add photo for item 30")).toBeTruthy();
    expect(container.querySelector('[data-testid="item-photo-input-30"]')).toBeTruthy();
    expect(container.querySelector("img")).toBeNull();
  });

  it("pending → 'photo attached — screening…', no button, no image", () => {
    const { container, queryByLabelText } = withPhoto({ photo_status: "pending" });
    expect(container.textContent).toContain("screening");
    expect(queryByLabelText("Add photo for item 30")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
  });

  it("clean → 'photo on file ✓' with the one-time scale-in class, no image", () => {
    const { container, queryByLabelText } = withPhoto({ photo_status: "clean" });
    expect(container.textContent).toContain("photo on file ✓");
    expect(container.querySelector(".checklist-photo-filed")).toBeTruthy();
    expect(queryByLabelText("Add photo for item 30")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
  });

  it("refused → the refusal copy + a retry affordance ('Add a different photo')", () => {
    const { container, getByLabelText } = withPhoto({ photo_status: "refused" });
    expect(container.textContent).toContain("refused by the security screen");
    expect(getByLabelText("Add photo for item 30")).toBeTruthy();
    expect(getByLabelText("Add photo for item 30").textContent).toContain("different photo");
  });

  it("upload flow: encodePhoto → uploadItemPhoto → onPhotoUploaded refetch", async () => {
    vi.mocked(encodePhoto).mockResolvedValue({ data: "x", name: "p.jpg", taken_at: "", gps: "" });
    vi.mocked(checklist.uploadItemPhoto).mockResolvedValue({
      ok: true, photo_id: 7, photo_status: "pending", photo_ref: "pending:7",
    });
    const onPhotoUploaded = vi.fn();

    const { container } = withPhoto({ photo_status: null }, { onPhotoUploaded });
    const input = container.querySelector('[data-testid="item-photo-input-30"]') as HTMLInputElement;
    const file = new File([new Uint8Array([1, 2, 3])], "p.jpg", { type: "image/jpeg" });
    fireEvent.change(input, { target: { files: [file] } });

    await waitFor(() =>
      expect(checklist.uploadItemPhoto).toHaveBeenCalledWith(30, expect.objectContaining({ name: "p.jpg" })),
    );
    await waitFor(() => expect(onPhotoUploaded).toHaveBeenCalled());
  });

  it("upload error surfaces the human copy (e.g. the one-photo 409) inline, never silently", async () => {
    vi.mocked(encodePhoto).mockResolvedValue({ data: "x", name: "p.jpg", taken_at: "", gps: "" });
    vi.mocked(checklist.uploadItemPhoto).mockRejectedValue(new ApiError("photo_already_attached", 409));

    const { container } = withPhoto({ photo_status: null });
    const input = container.querySelector('[data-testid="item-photo-input-30"]') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [new File([new Uint8Array([1])], "p.jpg")] } });

    await waitFor(() => expect(container.textContent).toContain("one photo per item"));
  });

  it("no photo affordance on a form_linked item even when opted in (its evidence is the filed form)", () => {
    const { container } = renderRow(
      makeItem({ id: 31, item_type: "form_linked", form_code: "daily-report", target_count: null }),
      { onPhotoUploaded: vi.fn(), canOpenForm: false },
    );
    expect(container.querySelector('[data-testid="item-photo-input-31"]')).toBeNull();
  });
});

describe("ChecklistItemRow — form_linked / inspection dead-ends + Filed state", () => {
  const linked = (over: Partial<checklist.ChecklistItemState> = {}) =>
    makeItem({
      id: 11, item_type: "form_linked", label: "File the Daily Field Report",
      form_code: "daily-report", target_count: null, ...over,
    });

  it("an unknown/retired form_code renders the dead-end explanation instead of a button", () => {
    const { container } = renderRow(linked({ form_code: "retired-form-x" }));
    expect(container.textContent ?? "").toContain(DEADEND_FORM_UNAVAILABLE);
    expect(container.querySelector("button")).toBeNull();
  });

  it("a missing form_code renders the same dead-end explanation", () => {
    const { container } = renderRow(linked({ form_code: null }));
    expect(container.textContent ?? "").toContain(DEADEND_FORM_UNAVAILABLE);
    expect(container.querySelector("button")).toBeNull();
  });

  it("canOpenForm=false (assigned instance without job+date) explains instead of a dead button", () => {
    const { container } = renderRow(linked(), { canOpenForm: false });
    expect(container.textContent ?? "").toContain(DEADEND_NO_JOB_DATE);
    expect(container.querySelector("button")).toBeNull();
  });

  it("an openable deep-link keeps the primary Complete button firing onOpenForm", () => {
    const item = linked();
    const { getByLabelText, props } = renderRow(item);
    fireEvent.click(getByLabelText("Complete File the Daily Field Report"));
    expect(props.onOpenForm).toHaveBeenCalledWith(item);
  });

  it("done: a static Filed ✓ pill + a small 'File another' link (no primary re-file button)", () => {
    const item = linked({ status: "done", completed_by: "(auto)", filed_by: "Mo Manager" });
    const { getByLabelText, queryByLabelText, container, props } = renderRow(item);
    expect(container.textContent ?? "").toContain("Filed ✓");
    expect(container.textContent ?? "").toContain("filed by Mo Manager");
    expect(queryByLabelText("Complete File the Daily Field Report")).toBeNull();
    const link = getByLabelText("File another File the Daily Field Report");
    // Quiet secondary, never the primary — re-filing an already-filed form is not the
    // obvious next action. (Was --ghost, which is the white-on-green HEADER variant and
    // rendered invisible on this white card; --secondary is the light-surface equivalent.)
    expect(link.className).toContain("btn--secondary");
    expect(link.className).not.toContain("btn--primary");
    fireEvent.click(link);
    expect(props.onOpenForm).toHaveBeenCalledWith(item);
  });

  it("done but unopenable: pill only, no File another link", () => {
    const item = linked({ status: "done", form_code: "retired-form-x" });
    const { queryByLabelText, container } = renderRow(item);
    expect(container.textContent ?? "").toContain("Filed ✓");
    expect(queryByLabelText("File another File the Daily Field Report")).toBeNull();
  });
});
