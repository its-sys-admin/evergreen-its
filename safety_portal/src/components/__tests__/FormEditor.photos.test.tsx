import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Field, FormDefinition, Section } from "../../forms/types";
import { SECTION_TYPES, photosMacroSection } from "../../forms/editorModel";
import { FormEditor } from "../FormEditor";

afterEach(cleanup);

// ─────────────────────────────────────────────────────────────────────────────
// Photos program (2026-08-27) — the builder's photo surfaces:
//   • "+ Photos" MACRO: inserts a titled header section carrying ONE optional inline photo
//     field (no max_count → server default 4); a second insertion suffixes the key.
//   • "+ Additional photos (pool)" palette item (SECTION_TYPES' 8th member): disabled with a
//     reason once the draft carries a mount (client half of the ONE-MOUNT rule — server half
//     is worker/publishValidation.ts).
//   • The pool's section editor: editable title, a READ-ONLY fixed-key display, and NO key
//     input (the key is the wire key by construction).
//   • FieldEditor "Max photos": photo inputs only; 4 (the default) OMITS max_count; switching
//     the input away from photo DELETES max_count (publishValidation rejects it otherwise).
// ─────────────────────────────────────────────────────────────────────────────

function makeDef(sections: Section[]): FormDefinition {
  return {
    form_code: "probe-v1",
    parent_form_code: "probe",
    form_name: "Probe",
    variant_label: null,
    version: 1,
    archetype: "sectioned_assessment",
    source_pdf: "",
    sections,
  };
}

function renderEditor(def: FormDefinition, onChange = vi.fn()) {
  const utils = render(
    <FormEditor
      def={def}
      onChange={onChange}
      mode="create"
      identity="probe"
      onIdentityChange={vi.fn()}
      parentFormCode="probe"
      onParentChange={vi.fn()}
      knownParents={["probe"]}
      category="safety"
      onCategoryChange={vi.fn()}
    />,
  );
  return { onChange, ...utils };
}

const FREEFORM: Section = { type: "freeform", key: "notes", label: "Notes" };
const POOL: Section = { type: "additional_photos", key: "additional_photos", title: "Additional photos" };

function lastSections(onChange: ReturnType<typeof vi.fn>): Section[] {
  const calls = onChange.mock.calls;
  expect(calls.length).toBeGreaterThan(0);
  return (calls[calls.length - 1][0] as FormDefinition).sections;
}

describe("the '+ Photos' macro", () => {
  it("inserts a titled header section with ONE optional photo field (no max_count)", () => {
    const { onChange, getByText } = renderEditor(makeDef([FREEFORM]));
    fireEvent.click(getByText("+ Photos"));
    const sections = lastSections(onChange);
    expect(sections).toHaveLength(2);
    expect(sections[1]).toEqual({
      type: "header",
      title: "Photos",
      fields: [{ key: "photos", label: "Photos", input: "photo" }],
    });
    const field = (sections[1] as Extract<Section, { type: "header" }>).fields[0];
    expect("required" in field).toBe(false); // optional — decision 6 (photos never required)
    expect("max_count" in field).toBe(false); // server default (4) by omission
  });

  it("a second insertion suffixes the field key (photos → photos_2)", () => {
    const { onChange, getByText } = renderEditor(
      makeDef([FREEFORM, photosMacroSection(new Set(["notes"]))]),
    );
    fireEvent.click(getByText("+ Photos"));
    const sections = lastSections(onChange);
    const added = sections[2] as Extract<Section, { type: "header" }>;
    expect(added.fields[0].key).toBe("photos_2");
  });
});

describe("the pool palette item + the one-mount disable", () => {
  it("SECTION_TYPES carries additional_photos as its 8th (composable) member", () => {
    expect(SECTION_TYPES[SECTION_TYPES.length - 1]).toBe("additional_photos");
  });

  it("offers '+ Additional photos (pool)' and inserts the fixed-key section", () => {
    const { onChange, getByText } = renderEditor(makeDef([FREEFORM]));
    const btn = getByText("+ Additional photos (pool)");
    expect((btn as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(btn);
    const sections = lastSections(onChange);
    expect(sections[1]).toEqual(POOL);
  });

  it("disables the pool button (with the reason) once the draft already has a mount", () => {
    const { getByText } = renderEditor(makeDef([FREEFORM, POOL]));
    const btn = getByText("+ Additional photos (pool)") as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    expect(btn.title).toMatch(/at most one per form/i);
    expect(btn.getAttribute("aria-label")).toMatch(/at most one per form/i);
    // The macro stays available regardless — inline photo fields are not the pool.
    expect((getByText("+ Photos") as HTMLButtonElement).disabled).toBe(false);
  });
});

describe("the pool section editor (title-only)", () => {
  it("shows an editable Title + the read-only fixed key, and NO key input", () => {
    const onChange = vi.fn();
    const { container, getByText } = renderEditor(makeDef([POOL]), onChange);

    // The fixed wire key renders as read-only <output> with the explanatory hint. (The
    // identity panel's derived form-code is ALSO an <output> — scope to the section list.)
    const outs = Array.from(
      container.querySelectorAll(".form-editor__section-list output.form-editor__derived"),
    );
    expect(outs.map((o) => o.textContent)).toEqual(["additional_photos"]);
    expect(getByText(/Fixed wire key/)).toBeTruthy();
    expect(getByText(/At most one\s+per form/)).toBeTruthy();

    // No editable key input anywhere (the only section is the pool).
    expect(getByText(/Section key \(fixed\)/)).toBeTruthy();
    const inputs = Array.from(container.querySelectorAll("input.field__input"));
    // Identity-panel inputs exist; none of them (nor any section input) edits the pool key.
    for (const i of inputs) expect((i as HTMLInputElement).value).not.toBe("additional_photos");

    // Title edits round-trip through onChange.
    const titleInput = inputs.find((i) => (i as HTMLInputElement).value === "Additional photos");
    expect(titleInput).toBeTruthy();
    fireEvent.change(titleInput as HTMLInputElement, { target: { value: "More site photos" } });
    const sections = lastSections(onChange);
    expect(sections[0]).toEqual({ ...POOL, title: "More site photos" });
  });

  it("keeps Remove/Move controls (composable — not definition-managed)", () => {
    const { getByLabelText, queryByText } = renderEditor(makeDef([FREEFORM, POOL]));
    expect(getByLabelText("Remove section 2")).toBeTruthy();
    expect(getByLabelText("Move section 2 up")).toBeTruthy();
    expect(queryByText("definition-managed")).toBeNull();
  });
});

describe("FieldEditor — the Max photos select", () => {
  function headerDef(field: Field): FormDefinition {
    return makeDef([{ type: "header", fields: [field] }]);
  }
  function maxPhotosSelect(container: HTMLElement): HTMLSelectElement | null {
    const label = Array.from(container.querySelectorAll("label")).find((l) =>
      l.textContent?.includes("Max photos"),
    );
    return label ? label.querySelector("select") : null;
  }

  it("renders ONLY for photo inputs", () => {
    const photo = renderEditor(headerDef({ key: "photos", label: "Photos", input: "photo" }));
    expect(maxPhotosSelect(photo.container as HTMLElement)).not.toBeNull();
    cleanup();
    const text = renderEditor(headerDef({ key: "notes", label: "Notes", input: "text" }));
    expect(maxPhotosSelect(text.container as HTMLElement)).toBeNull();
  });

  it("defaults to '4 (default)' and choosing 4 OMITS max_count", () => {
    const { container, onChange } = renderEditor(
      headerDef({ key: "photos", label: "Photos", input: "photo", max_count: 2 }),
    );
    const select = maxPhotosSelect(container as HTMLElement)!;
    expect(select.value).toBe("2");
    fireEvent.change(select, { target: { value: "4" } });
    const sections = lastSections(onChange);
    const field = (sections[0] as Extract<Section, { type: "header" }>).fields[0];
    expect("max_count" in field).toBe(false); // 4 = the server default — never pinned
  });

  it("choosing 1..3 sets max_count on the field", () => {
    const { container, onChange } = renderEditor(
      headerDef({ key: "photos", label: "Photos", input: "photo" }),
    );
    const select = maxPhotosSelect(container as HTMLElement)!;
    expect(select.value).toBe("4"); // no max_count → the default
    fireEvent.change(select, { target: { value: "2" } });
    const sections = lastSections(onChange);
    const field = (sections[0] as Extract<Section, { type: "header" }>).fields[0];
    expect(field.max_count).toBe(2);
  });

  it("switching the input away from photo DELETES max_count (publish would reject it)", () => {
    const { container, onChange } = renderEditor(
      headerDef({ key: "photos", label: "Photos", input: "photo", max_count: 3 }),
    );
    const inputSelect = Array.from(container.querySelectorAll("label"))
      .find((l) => l.textContent?.startsWith("Input"))!
      .querySelector("select")!;
    fireEvent.change(inputSelect, { target: { value: "text" } });
    const sections = lastSections(onChange);
    const field = (sections[0] as Extract<Section, { type: "header" }>).fields[0];
    expect(field.input).toBe("text");
    expect("max_count" in field).toBe(false);
  });
});
