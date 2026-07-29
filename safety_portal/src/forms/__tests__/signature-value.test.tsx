/**
 * Signature values flow THROUGH FormRenderer into the pad — the wiring layer where the
 * amend defect actually shipped.
 *
 * SignaturePad held its committed signature in internal state only, so nothing external
 * could set it. Two consequences, both silent and both landing in a filed legal PDF:
 *
 *   1. AMEND — `loadAmend` rewrites `values` WITHOUT remounting the pad, so the pad kept
 *      rendering blank over a real, submittable signature. Once the inline element became
 *      a read-only preview (the full-screen-sheet change), blank reads strongly as "not
 *      signed": a user could amend, never tap it, submit, and file the amended PDF
 *      carrying the OLD ink while the UI said "Tap to sign".
 *   2. ROW REMOVAL — `signature_table` rows are index-keyed (`key={i}`), so removing a row
 *      shifts values up while React REUSES the component instance. An uncontrolled pad
 *      then paints the deleted person's signature onto the next person's row.
 *
 * A SYNTHETIC definition keeps this version-proof, and deliberately puts a signature
 * column in a plain `repeating_table` as well as a `signature_table` — `incident-report-v3`
 * really does that, and both are served by the same TableView branch.
 *
 * Nothing else covers a signature value flowing through FormRenderer; that gap is why the
 * bug shipped.
 */
import { cleanup, fireEvent, render } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { FormRenderer, initialValues, type FormValues } from "../FormRenderer";
import type { FormDefinition } from "../types";

afterEach(cleanup);

const SIG_A = "M 10 20 L 30 40 L 60 25";
const SIG_B = "M 5 5 L 15 15";
const SIG_C = "M 90 10 L 120 60";

const DEF: FormDefinition = {
  form_code: "sig-value-test-v1",
  parent_form_code: "sig-value-test",
  form_name: "Signature value test",
  variant_label: null,
  version: 1,
  archetype: "checklist",
  source_pdf: "n/a",
  sections: [
    {
      type: "header",
      key: "signoff",
      title: "Sign-off",
      fields: [{ key: "supervisor_sig", label: "Supervisor signature", input: "signature" }],
    },
    {
      type: "signature_table",
      key: "crew",
      title: "Crew sign-in",
      min_rows: 3,
      allow_add: true,
      columns: [
        { key: "name", label: "Name", input: "text" },
        { key: "sig", label: "Signature", input: "signature" },
      ],
    },
  ],
} as unknown as FormDefinition;

/** Mirrors the real hosts: FormRenderer writes through a setter the page owns. */
function Harness({ initial, amendTo }: { initial: FormValues; amendTo?: FormValues }) {
  const [values, setValues] = useState<FormValues>(initial);
  return (
    <>
      {amendTo ? (
        <button type="button" onClick={() => setValues(amendTo)}>
          load amendment
        </button>
      ) : null}
      <FormRenderer def={DEF} values={values} setValues={setValues} />
    </>
  );
}

/** Every pad's preview `d`, in DOM order: [header, row0, row1, row2, ...]. */
function previewPaths(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll('svg[role="img"] path')).map(
    (p) => p.getAttribute("d") ?? "",
  );
}

describe("signature values reach the pad through FormRenderer", () => {
  it("a seeded HEADER signature renders in its pad", () => {
    const values = { ...initialValues(DEF), supervisor_sig: SIG_A };
    const { container } = render(<Harness initial={values} />);
    expect(previewPaths(container)[0]).toBe(SIG_A);
  });

  it("a seeded TABLE-CELL signature renders in its pad", () => {
    const base = initialValues(DEF);
    const rows = (base.crew as Array<Record<string, string>>).map((r, i) => ({
      ...r,
      sig: [SIG_A, SIG_B, SIG_C][i] ?? "",
    }));
    const { container } = render(<Harness initial={{ ...base, crew: rows }} />);
    expect(previewPaths(container).slice(1)).toEqual([SIG_A, SIG_B, SIG_C]);
  });

  it("AMEND: values replaced WITHOUT a remount show the prior signature, not a blank pad", () => {
    const base = initialValues(DEF);
    const amended = { ...base, supervisor_sig: SIG_A };
    const { container, getByText } = render(<Harness initial={base} amendTo={amended} />);

    expect(previewPaths(container)[0]).toBe(""); // fresh form
    expect(container.querySelector(".sig__hint")?.textContent).toBe("Tap to sign");

    fireEvent.click(getByText("load amendment")); // exactly what loadAmend does

    expect(previewPaths(container)[0]).toBe(SIG_A);
    expect(
      container.querySelector(".sig__hint")?.textContent,
      "a preview reading 'Tap to sign' over a real signature is the whole defect",
    ).toBe("Tap to edit");
  });

  it("ROW REMOVAL: index-keyed rows shift values up without dragging signatures along", () => {
    const base = initialValues(DEF);
    const rows = (base.crew as Array<Record<string, string>>).map((r, i) => ({
      ...r,
      name: ["Ana", "Ben", "Cal"][i] ?? "",
      sig: [SIG_A, SIG_B, SIG_C][i] ?? "",
    }));
    const { container, getByLabelText } = render(<Harness initial={{ ...base, crew: rows }} />);
    expect(previewPaths(container).slice(1)).toEqual([SIG_A, SIG_B, SIG_C]);

    fireEvent.click(getByLabelText("Remove row 1")); // drop Ana

    // React reuses the instance at index 0; an uncontrolled pad kept painting Ana's ink
    // onto Ben's row — the wrong person's signature on a legal document.
    expect(previewPaths(container).slice(1)).toEqual([SIG_B, SIG_C]);
  });
});
