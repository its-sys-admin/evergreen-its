/**
 * The daily SOP's chronological day-rail (design-refinement pass, 2026-07).
 *
 * PRESENTATIONAL contract:
 *   • dayPhaseFor is a pure heading→phase mapping: the five SOP phase openers get
 *     their time-of-day eyebrow; continuation and non-SOP headings get null;
 *   • with FormRenderer's `dayRail` prop (the Daily tab), every guidance section
 *     carries the rail class and exactly the five phase openers carry an eyebrow —
 *     against BOTH the daily-report-v2 baseline and the current v5 definition (the
 *     openers are the SOP's own wording, stable across versions);
 *   • WITHOUT the prop (the generic fill page and every other form) the guidance
 *     markup is unchanged — no rail class, no eyebrow. The rail is chrome, not state:
 *     initialValues is untouched by it (guidance stays keyless by design).
 */
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { dayPhaseFor } from "../dayPhase";
import { FormRenderer, initialValues } from "../FormRenderer";
import { formCatalog, getDefinition, getDefinitionFor } from "../registry";
import type { FormDefinition } from "../types";

afterEach(cleanup);

const PHASES_IN_DAY_ORDER = [
  "7:30 AM",
  "MORNING KICKOFF",
  "THROUGH THE DAY",
  "CHECK-INS",
  "END OF DAY",
];

describe("dayPhaseFor — pure heading→phase mapping", () => {
  it("maps the five SOP phase-opening headings (v2 wording, verbatim)", () => {
    expect(dayPhaseFor("7:30 AM — Arrive On Site — You Set the Tone")).toBe("7:30 AM");
    expect(dayPhaseFor("A. Morning Kickoff — 1. Sign Workers In")).toBe("MORNING KICKOFF");
    expect(
      dayPhaseFor("D. Throughout the Day — 12. Photo Documentation — Minimum 50 Photos Per Day"),
    ).toBe("THROUGH THE DAY");
    expect(dayPhaseFor("E. Check-Ins with Construction Manager — 2x Per Day (Minimum)")).toBe(
      "CHECK-INS",
    );
    expect(dayPhaseFor("END OF DAY — Before Leaving the Site")).toBe("END OF DAY");
  });

  it("returns null for continuation and non-phase headings", () => {
    expect(dayPhaseFor("SITE SUPERVISOR — STANDARD OPERATING PROCEDURE")).toBeNull();
    expect(dayPhaseFor("2. PPE Verification")).toBeNull();
    expect(dayPhaseFor("B. OSHA Compliance & Ongoing Safety Oversight — 5. Trenching & Excavation (29 CFR 1926 Subpart P)")).toBeNull();
    expect(dayPhaseFor("C. Quality Control — Verifying the Work")).toBeNull();
    expect(dayPhaseFor("F. General Expectations & Standards of Conduct")).toBeNull();
    expect(dayPhaseFor("")).toBeNull();
    expect(dayPhaseFor("Anything else entirely")).toBeNull();
  });

  it("is stable across the shipped daily definitions: every version's guidance headings yield exactly the five phases, in day order", async () => {
    // v2 is HISTORICAL (outside the post-split sync eager window) — resolve both
    // through the async pool path, which serves eager and lazy codes alike.
    for (const code of ["daily-report-v2", "daily-report-v5"]) {
      const def = (await getDefinitionFor(code)) as FormDefinition;
      expect(def, code).not.toBeNull();
      const phases = def.sections
        .filter((s) => s.type === "guidance")
        .map((s) => dayPhaseFor((s as { heading: string }).heading))
        .filter((ph): ph is NonNullable<typeof ph> => ph !== null);
      expect(phases, code).toEqual(PHASES_IN_DAY_ORDER);
    }
  });
});

describe("FormRenderer dayRail gating", () => {
  // Resolve the CURRENT daily report from the catalog rather than naming a version. The eager
// registry window is "current + immediately-previous" (vite-plugin-eager-forms), so a
// hard-coded code silently becomes null two cuts later — which is exactly what v7 did to the
// v5 references here. These tests are about renderer behaviour, not a historical version.
const DEF = getDefinition(
  formCatalog().find((p) => p.parent_form_code === "daily-report")!.form_code!,
) as FormDefinition;

  it("with dayRail: every guidance section is railed; exactly the five openers carry an eyebrow", () => {
    const { container } = render(
      <FormRenderer def={DEF} values={initialValues(DEF)} setValues={() => {}} dayRail />,
    );
    const guidance = container.querySelectorAll(".fr__guidance");
    const railed = container.querySelectorAll(".fr__guidance--rail");
    expect(guidance.length).toBeGreaterThan(0);
    expect(railed.length).toBe(guidance.length);

    const eyebrows = [...container.querySelectorAll(".fr__day-eyebrow")];
    expect(eyebrows.map((el) => el.textContent)).toEqual(PHASES_IN_DAY_ORDER);
    // Presentational restatement of the heading — hidden from the accessibility tree.
    for (const el of eyebrows) expect(el.getAttribute("aria-hidden")).toBe("true");
  });

  it("without dayRail (the generic fill page): no rail class, no eyebrows — markup unchanged", () => {
    const { container } = render(
      <FormRenderer def={DEF} values={initialValues(DEF)} setValues={() => {}} />,
    );
    expect(container.querySelectorAll(".fr__guidance").length).toBeGreaterThan(0);
    expect(container.querySelector(".fr__guidance--rail")).toBeNull();
    expect(container.querySelector(".fr__day-eyebrow")).toBeNull();
  });

  it("contributes NO fill state: initialValues is identical with or without the rail (guidance stays keyless)", () => {
    // dayRail is a render prop, not a values concern — but pin the invariant the
    // submit path relies on: guidance sections still contribute no keys at all.
    const keys = new Set(Object.keys(initialValues(DEF)));
    for (const s of DEF.sections) {
      if (s.type === "guidance") {
        expect("key" in s ? keys.has((s as { key?: string }).key ?? "") : false).toBe(false);
      }
    }
  });
});
