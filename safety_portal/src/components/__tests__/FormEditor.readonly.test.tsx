import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { FormDefinition } from "../../forms/types";
import { READ_ONLY_SECTION_TYPES } from "../../forms/editorModel";
import { FormEditor } from "../FormEditor";

afterEach(cleanup);

// Slice 1 (R3-F3) — the builder must not even OFFER the amputation the C3 gates now refuse:
// definition-managed (read-only) section types (guidance / form_link / job_requirements /
// expected_materials) get NO Remove/Move controls, while ordinary sections keep them. The
// server-side floor (required-content.json required_section_types, enforced in
// worker/publishValidation.ts + safety_reports/publish_manifest.py) is the boundary; this
// is the UI half.

function makeDef(): FormDefinition {
  return {
    form_code: "daily-report-v6",
    parent_form_code: "daily-report",
    form_name: "Daily Field Report",
    variant_label: null,
    version: 6,
    archetype: "rows_signatures",
    source_pdf: "",
    sections: [
      { type: "freeform", key: "comments", label: "Comments" },
      { type: "guidance", heading: "A. Morning", blocks: [{ type: "p", text: "x" }] },
      { type: "form_link", label: "Create a JHA", parent_form_code: "jha" },
      { type: "job_requirements", key: "job_requirements", title: "Job-specific requirements" },
      { type: "expected_materials", key: "expected_materials_receipt", title: "Expected materials" },
      { type: "additional_photos", key: "additional_photos", title: "Additional site photos" },
    ],
  };
}

function renderEditor(def: FormDefinition) {
  return render(
    <FormEditor
      def={def}
      onChange={vi.fn()}
      mode="edit"
      identity="daily-report"
      onIdentityChange={vi.fn()}
      parentFormCode="daily-report"
      onParentChange={vi.fn()}
      knownParents={["daily-report", "jha"]}
      category="progress"
      onCategoryChange={vi.fn()}
    />,
  );
}

describe("FormEditor — read-only sections have no Remove/Move controls (Slice 1, R3-F3)", () => {
  it("covers all five definition-managed types (guard against list drift)", () => {
    expect([...READ_ONLY_SECTION_TYPES].sort()).toEqual([
      "additional_photos",
      "expected_materials",
      "form_link",
      "guidance",
      "job_requirements",
    ]);
  });

  it("suppresses Remove/Move on read-only sections; ordinary sections keep them", () => {
    const { getByLabelText, queryByLabelText, getAllByText } = renderEditor(makeDef());

    // Section 1 (freeform) is ordinary — its controls are present.
    expect(getByLabelText("Move section 1 up")).toBeTruthy();
    expect(getByLabelText("Move section 1 down")).toBeTruthy();
    expect(getByLabelText("Remove section 1")).toBeTruthy();

    // Sections 2-5 are definition-managed — NO Remove/Move controls, a lock note instead.
    for (const i of [2, 3, 4, 5]) {
      expect(queryByLabelText(`Move section ${i} up`)).toBeNull();
      expect(queryByLabelText(`Move section ${i} down`)).toBeNull();
      expect(queryByLabelText(`Remove section ${i}`)).toBeNull();
    }
    expect(getAllByText("definition-managed")).toHaveLength(5);
  });

  // The teeth for the bug this file exists to prevent. `sectionEditor` returns undefined for an
  // unhandled type and the project does not set `noImplicitReturns`, so a missing case renders a
  // BLANK section body instead of failing the build — which is how `additional_photos` shipped
  // unhandled in FormEditor.tsx and editorValidation.ts while editorModel.ts already knew it.
  //
  // Driven off READ_ONLY_SECTION_TYPES rather than a hand-written list, so a type added to the
  // set without a matching case fails HERE.
  it("EVERY definition-managed type renders the read-only explanation (not a blank body)", () => {
    const sample: Record<string, Record<string, unknown>> = {
      guidance: { type: "guidance", heading: "A. Morning", blocks: [{ type: "p", text: "x" }] },
      form_link: { type: "form_link", label: "Create a JHA", parent_form_code: "jha" },
      job_requirements: { type: "job_requirements", key: "job_requirements", title: "R" },
      expected_materials: { type: "expected_materials", key: "expected_materials_receipt", title: "M" },
      additional_photos: { type: "additional_photos", key: "additional_photos", title: "P" },
    };
    for (const type of READ_ONLY_SECTION_TYPES) {
      const def = makeDef();
      const section = sample[type];
      expect(section, `no fixture for read-only type "${type}"`).toBeTruthy();
      def.sections = [section as unknown as FormDefinition["sections"][number]];
      const { getAllByText, unmount } = renderEditor(def);
      expect(
        getAllByText(/maintained in the form definition/i).length,
        `"${type}" rendered no read-only explanation — a missing case in FormEditor.sectionEditor`,
      ).toBe(1);
      unmount();
    }
  });

  it("a definition of ONLY ordinary sections keeps all controls (no regression)", () => {
    const def = makeDef();
    def.sections = [
      { type: "freeform", key: "one", label: "One" },
      { type: "freeform", key: "two", label: "Two" },
    ];
    const { getByLabelText } = renderEditor(def);
    expect(getByLabelText("Remove section 1")).toBeTruthy();
    expect(getByLabelText("Remove section 2")).toBeTruthy();
    expect(getByLabelText("Move section 2 up")).toBeTruthy();
  });
});
