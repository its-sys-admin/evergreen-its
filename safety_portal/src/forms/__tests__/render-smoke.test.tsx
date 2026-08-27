/**
 * SPA render-smoke net — the THIRD renderer (Phase-2 slice 3c, design brief C5).
 *
 * The Python sibling (tests/test_render_smoke.py) renders every ACTIVE catalog form
 * through the TWO Python renderers (render_submission_pdf / render_blank_fillable) and
 * asserts NON-DEGRADED output — real structural content, not "no exception". This file
 * is the third leg: it renders every active form through the SPA <FormRenderer/> (React)
 * in jsdom and holds the rendered DOM to the SAME structural contract. All three
 * renderers (two Python + this one) of the one form definition must produce the form's
 * real structure; a renderer that silently drops a section / label / field is caught
 * HERE, before it ships — the safety net that makes the no-human-merge auto-publish
 * (brief C12) safe.
 *
 * Non-degraded strategy (mirrors the Python needle approach):
 *   1. COUNTS — one rendered section container per non-null definition section; the
 *      expected field/input count per header, cell count per table, and item count per
 *      checklist. A dropped section or field changes a count and fails here.
 *   2. NEEDLES — a representative subset of section titles / field labels / checklist
 *      group + item labels / static + content text must be present in the DOM text.
 *      A renderer that mangles or omits a label loses a needle and fails here.
 *
 * Renderer facts modeled so the assertions match ACTUAL FormRenderer behavior (an
 * assertion the renderer is designed never to satisfy would be a false alarm):
 *   * ENVELOPE_KEYS ("work_date", "job") header fields are SKIPPED by the renderer (the
 *     fill page provides them) — so they are NOT counted and NOT expected as needles. A
 *     header whose fields are ALL envelope-bound renders nothing (no section container).
 *   * A `static_text` section renders a <p>, NOT a `.fr__section` container — so it does
 *     not add to the section-container count (but its text IS an expected needle).
 *   * Signature fields/cells render a SignaturePad (an <svg role="img"> + Clear button),
 *     NOT a plain <input> — counted as a control, by label, not as an `<input>`.
 *
 * Negative control (load-bearing proof): `test_negative_control_detects_degradation`
 * below renders a definition with a section deliberately stripped of its fields and
 * asserts the SAME count/needle machinery FAILS — so a future agent can see this net
 * actually catches a dropped section, not just that "render didn't throw". If that test
 * ever passes-by-doing-nothing, the net has rotted.
 *
 * Preservation (Op Stds §14): ADDITIVE. FormRenderer / registry / types are untouched;
 * this drives them exactly as the fill page does. The active set is read from
 * safety_portal/catalog.json (current_form_code per active form — NOT a forms/*.json
 * glob), so retired/inactive forms are never smoke-tested — same source of truth the
 * Python sibling and the portal's own form picker use.
 */
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import catalog from "../../../catalog.json";
import { FormRenderer, initialValues } from "../FormRenderer";
import { getDefinition } from "../registry";
import type { FormDefinition, Section } from "../types";

// ── manifest-driven active-form discovery (mirrors test_render_smoke.py) ─────────
interface CatalogForm {
  status: string;
  current_form_code: string;
}
interface CatalogParent {
  parent_form_code: string;
  forms: CatalogForm[];
}
interface Catalog {
  parents: CatalogParent[];
}

function activeFormCodes(): string[] {
  const manifest = catalog as Catalog;
  const codes: string[] = [];
  for (const parent of manifest.parents ?? []) {
    for (const form of parent.forms ?? []) {
      if (form.status === "active") {
        expect(form.current_form_code, `active form in ${parent.parent_form_code} has no current_form_code`).toBeTruthy();
        codes.push(form.current_form_code);
      }
    }
  }
  return codes;
}

const ACTIVE_FORM_CODES = activeFormCodes();

// The renderer's own envelope-key skip set (kept in sync with FormRenderer's private
// ENVELOPE_KEYS — the fill page provides job + work_date, so the renderer omits them).
const ENVELOPE_KEYS = new Set(["work_date", "job"]);

afterEach(cleanup);

// ── expected structural counts the rendered DOM MUST carry ──────────────────────
// Each count maps 1:1 to a definition structure and to ONE stable DOM wrapper class, so
// the count is unaffected by OPTIONAL renderer chrome (e.g. a checklist item's per-item
// "Comments" input, the "+ Add row" / row-remove buttons). A dropped section/field
// changes exactly one count.
interface ExpectedCounts {
  /** `.fr__section` containers — every section EXCEPT static_text (a bare <p>) and an
   *  all-envelope-bound header (renders null). */
  sections: number;
  /** `.field` wrappers — one per visible header field + one per freeform textarea. */
  fields: number;
  /** `.fr__cell` wrappers — one per (table row × column). */
  cells: number;
  /** `.fr__item` wrappers — one per checklist item. */
  items: number;
  /** `svg[role="img"]` SignaturePads — header signature fields + signature table cells. */
  signaturePads: number;
}

function headerVisibleFields(section: Extract<Section, { type: "header" }>) {
  return section.fields.filter((f) => !ENVELOPE_KEYS.has(f.key));
}

function expectedCounts(def: FormDefinition): ExpectedCounts {
  let sections = 0;
  let fields = 0;
  let cells = 0;
  let items = 0;
  let signaturePads = 0;
  const rowsFor = (s: Extract<Section, { type: "repeating_table" | "signature_table" }>) =>
    Math.max(1, s.min_rows ?? 1);

  for (const s of def.sections) {
    switch (s.type) {
      case "header": {
        const visible = headerVisibleFields(s);
        if (visible.length === 0) break; // renders null — no container
        sections += 1;
        for (const f of visible) {
          // Every header field is a `.field` wrapper; a signature is ALSO a SignaturePad.
          fields += 1;
          if (f.input === "signature") signaturePads += 1;
        }
        break;
      }
      case "static_text":
        break; // a <p>, not a section container
      case "freeform":
        sections += 1;
        fields += 1; // one <label class="field"> + <textarea>
        break;
      case "content_blocks":
        sections += 1; // text-only; no interactive control
        break;
      case "repeating_table":
      case "signature_table": {
        sections += 1;
        const rows = rowsFor(s);
        for (const c of s.columns) {
          cells += rows; // one `.fr__cell` per (row × column)
          if (c.input === "signature") signaturePads += rows;
        }
        break;
      }
      case "checklist": {
        sections += 1;
        for (const g of s.groups) items += g.items.length; // one `.fr__item` per item
        break;
      }
      case "guidance":
        sections += 1; // read-only SOP text — no interactive control
        break;
      case "form_link":
        sections += 1; // the "Create <form> →" button section — no value control
        break;
      case "additional_photos":
        // Since 2026-08-27 the adapterless render is a visible PLACEHOLDER section (title +
        // how-to-enable copy) — no longer null. job_requirements / expected_materials still
        // render null without their adapters (no case — falls through to zero).
        sections += 1;
        break;
    }
  }
  return { sections, fields, cells, items, signaturePads };
}

// ── expected structural needles (mirrors _expected_structural_strings) ──────────
function expectedNeedles(def: FormDefinition): string[] {
  const out: string[] = [];
  const add = (s: string | undefined | null) => {
    if (!s) return;
    const norm = s.replace(/\s+/g, " ").trim();
    if (norm) out.push(norm.slice(0, 40)); // cap like the Python sibling
  };

  for (const s of def.sections) {
    switch (s.type) {
      case "header":
        if ("title" in s) add(s.title);
        for (const f of headerVisibleFields(s)) add(f.label);
        break;
      case "static_text":
        add(s.text);
        break;
      case "freeform":
        add(s.label);
        break;
      case "content_blocks":
        add(s.title);
        for (const b of s.blocks.slice(0, 2)) {
          add(b.heading);
          add(b.body);
        }
        break;
      case "repeating_table":
      case "signature_table":
        add(s.title);
        for (const c of s.columns) add(c.label);
        break;
      case "checklist":
        add(s.title);
        for (const g of s.groups) {
          add(g.label);
          for (const it of g.items.slice(0, 3)) add(it.label);
        }
        break;
      case "guidance":
        // The SPA renders the FULL guidance prose (unlike the PDF's heading+callouts
        // truncation) — heading + a representative subset of block text are needles.
        add(s.heading);
        for (const b of s.blocks.slice(0, 2)) {
          if (b.type === "p" || b.type === "callout") add(b.text);
          else add(b.items[0]);
        }
        for (const b of s.blocks) if (b.type === "callout") add(b.text);
        break;
      case "form_link":
        add(s.label);
        break;
      case "additional_photos":
        // The adapterless placeholder renders the mount's title (default below mirrors
        // FormRenderer's fallback).
        add(s.title ?? "Additional site photos");
        break;
    }
  }
  // de-dupe, preserve order
  return [...new Set(out)];
}

// ── shared non-degraded assertion (counts + needles) ────────────────────────────
function assertNonDegraded(container: HTMLElement, def: FormDefinition, code: string): void {
  const counts = expectedCounts(def);

  // COUNTS: one container per non-null section, then the expected per-structure wrapper
  // counts (each selector unique + unaffected by optional comment/add-row chrome).
  expect(counts.sections, `${code}: definition produced zero section containers`).toBeGreaterThan(0);
  expect(container.querySelectorAll(".fr__section").length, `${code}: section-container count`).toBe(
    counts.sections,
  );
  expect(container.querySelectorAll(".field").length, `${code}: header/freeform field count`).toBe(
    counts.fields,
  );
  expect(container.querySelectorAll(".fr__cell").length, `${code}: table cell count`).toBe(
    counts.cells,
  );
  expect(container.querySelectorAll(".fr__item").length, `${code}: checklist item count`).toBe(
    counts.items,
  );

  // Signature pads: each SignaturePad renders one <svg role="img">.
  expect(
    container.querySelectorAll('svg[role="img"]').length,
    `${code}: signature-pad count`,
  ).toBe(counts.signaturePads);

  // NEEDLES: a representative subset of structural strings present in the DOM text.
  const text = (container.textContent ?? "").replace(/\s+/g, " ");
  const needles = expectedNeedles(def);
  expect(needles.length, `${code}: definition produced no structural needles`).toBeGreaterThan(0);
  const missing = needles.filter((n) => !text.includes(n));
  expect(
    missing,
    `${code}: render DEGRADED — expected structural strings absent from the DOM: ${JSON.stringify(
      missing.slice(0, 8),
    )}${missing.length > 8 ? ` (+${missing.length - 8} more)` : ""}`,
  ).toEqual([]);
}

// ── the manifest is non-empty + resolvable (guards a glob/parse regression) ─────
describe("active-form manifest", () => {
  it("discovers a non-empty active set, each resolvable to a bundled definition", () => {
    expect(ACTIVE_FORM_CODES.length, "no active forms discovered in catalog.json").toBeGreaterThan(0);
    for (const code of ACTIVE_FORM_CODES) {
      expect(getDefinition(code), `active form ${code} has no bundled definition`).not.toBeNull();
    }
  });
});

// ── every active form renders NON-DEGRADED through the SPA FormRenderer ──────────
describe("SPA FormRenderer renders every active form non-degraded", () => {
  it.each(ACTIVE_FORM_CODES)("%s", (code) => {
    const def = getDefinition(code);
    expect(def, `${code}: definition did not resolve`).not.toBeNull();
    const definition = def as FormDefinition;

    const { container } = render(
      <FormRenderer def={definition} values={initialValues(definition)} setValues={() => {}} />,
    );

    // A real render shell exists.
    expect(container.querySelector(".fr"), `${code}: no FormRenderer root rendered`).not.toBeNull();

    assertNonDegraded(container as HTMLElement, definition, code);
  });
});

// ── NEGATIVE CONTROL: prove the net actually catches a dropped section ───────────
// If FormRenderer (or the active set) ever changed such that this assertion machinery
// stopped detecting a missing section, this test would START PASSING — i.e. the net has
// rotted to "render didn't throw". It must FAIL on the degraded definition below.
describe("negative control — the net is load-bearing", () => {
  it("detects a degraded definition (a checklist group with all items dropped)", () => {
    // Take a real active form's definition and DROP every item from its first checklist
    // group (a renderer-level "silently dropped a section" simulated at the def level).
    const base = ACTIVE_FORM_CODES.map((c) => getDefinition(c)).find((d) =>
      d?.sections.some((s) => s.type === "checklist"),
    );
    expect(base, "no active form has a checklist to build a negative control from").not.toBeNull();
    const intact = base as FormDefinition;

    // Deep clone, then strip the first checklist group's items: the rendered DOM now
    // has fewer `.fr__item` controls AND is missing that group's item-label needles than
    // the ORIGINAL definition expects — so asserting the intact def's contract must fail.
    const degraded: FormDefinition = JSON.parse(JSON.stringify(intact));
    const cl = degraded.sections.find((s) => s.type === "checklist") as Extract<
      Section,
      { type: "checklist" }
    >;
    cl.groups[0].items = []; // drop the items

    const { container } = render(
      <FormRenderer def={degraded} values={initialValues(degraded)} setValues={() => {}} />,
    );

    // Assert against the INTACT contract: the degraded render must violate it.
    expect(() => assertNonDegraded(container as HTMLElement, intact, intact.form_code)).toThrow();
  });
});
