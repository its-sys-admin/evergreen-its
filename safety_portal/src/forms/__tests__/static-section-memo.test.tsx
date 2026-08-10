/**
 * StaticSectionView memoization (optimization #5): the value-independent section types
 * (guidance / content_blocks / static_text) render through a React.memo'd component — a
 * keystroke re-render of FormRenderer must NOT re-render them (the SOP daily form carries
 * ~20 guidance sections that used to re-render per key press).
 *
 * Render counter: dayPhaseFor is called exactly once per guidance-section render when the
 * day-rail is on (the eyebrow derivation), so its spy call count is a faithful proxy for
 * "did the static sections re-render". vi.mock({ spy: true }) keeps the real implementation.
 * The day-rail markup contract itself is pinned by day-rail.test.tsx (unchanged).
 */
import { useState } from "react";
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../dayPhase", { spy: true });

import { dayPhaseFor } from "../dayPhase";
import { FormRenderer, initialValues } from "../FormRenderer";
import { formCatalog, getDefinition } from "../registry";
import type { FormDefinition } from "../types";

/** Owns the values state like a real host (Daily tab / fill page) so keystrokes re-render. */
function Harness({ def, dayRail }: { def: FormDefinition; dayRail?: boolean }) {
  const [values, setValues] = useState(() => initialValues(def));
  return <FormRenderer def={def} values={values} setValues={setValues} dayRail={dayRail} />;
}

afterEach(cleanup);
beforeEach(() => vi.clearAllMocks());

describe("StaticSectionView — memo short-circuit on keystroke re-renders", () => {
  // Resolve the CURRENT daily report from the catalog rather than naming a version. The eager
// registry window is "current + immediately-previous" (vite-plugin-eager-forms), so a
// hard-coded code silently becomes null two cuts later — which is exactly what v7 did to the
// v5 references here. These tests are about renderer behaviour, not a historical version.
const DEF = getDefinition(
  formCatalog().find((p) => p.parent_form_code === "daily-report")!.form_code!,
) as FormDefinition;
  const GUIDANCE_COUNT = DEF.sections.filter((s) => s.type === "guidance").length;

  it("a keystroke re-render does NOT re-render guidance sections (dayPhaseFor call count stays flat)", () => {
    const view = render(<Harness def={DEF} dayRail />);
    expect(GUIDANCE_COUNT).toBeGreaterThan(0);
    expect(vi.mocked(dayPhaseFor)).toHaveBeenCalledTimes(GUIDANCE_COUNT); // one derivation per mount
    const weather = view.getByLabelText("Weather") as HTMLInputElement;
    fireEvent.change(weather, { target: { value: "Clear" } });
    fireEvent.change(weather, { target: { value: "Clear skies" } });
    expect(weather.value).toBe("Clear skies"); // the parent DID re-render with the new values…
    expect(vi.mocked(dayPhaseFor)).toHaveBeenCalledTimes(GUIDANCE_COUNT); // …the static sections did not
  });

  it("the rail/eyebrow chrome survives the memo extraction (present with dayRail, absent without)", () => {
    const railed = render(<Harness def={DEF} dayRail />);
    expect(railed.container.querySelectorAll(".fr__guidance--rail").length).toBe(GUIDANCE_COUNT);
    expect(railed.container.querySelectorAll(".fr__day-eyebrow").length).toBe(5); // the five phase openers
    cleanup();
    const plain = render(<Harness def={DEF} />);
    expect(plain.container.querySelectorAll(".fr__guidance").length).toBe(GUIDANCE_COUNT);
    expect(plain.container.querySelector(".fr__guidance--rail")).toBeNull();
    expect(plain.container.querySelector(".fr__day-eyebrow")).toBeNull();
  });

  it("static_text and content_blocks render through the memo path with unchanged markup", () => {
    const def: FormDefinition = {
      form_code: "memo-probe-v1",
      parent_form_code: "memo-probe",
      form_name: "Memo probe",
      variant_label: null,
      version: 1,
      archetype: "test",
      source_pdf: "n/a",
      sections: [
        { type: "header", fields: [{ key: "note", label: "Note", input: "text" }] },
        { type: "static_text", text: "Static line", emphasis: "legal" },
        { type: "content_blocks", key: "cb", blocks: [{ heading: "H", body: "Body copy" }] },
      ],
    };
    const view = render(<Harness def={def} />);
    expect(view.container.querySelector(".fr__static--legal")?.textContent).toBe("Static line");
    expect(view.container.querySelector(".fr__content-heading")?.textContent).toBe("H");
    expect(view.container.querySelector(".fr__content-body")?.textContent).toBe("Body copy");
    // …and a keystroke leaves them intact (values flow only to the interactive section).
    fireEvent.change(view.getByLabelText("Note"), { target: { value: "x" } });
    expect((view.getByLabelText("Note") as HTMLInputElement).value).toBe("x");
    expect(view.container.querySelector(".fr__content-body")?.textContent).toBe("Body copy");
  });
});
