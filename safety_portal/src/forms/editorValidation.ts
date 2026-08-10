// CLIENT-SIDE validation for the Form Editor — UX only. The Worker's
// worker/publishValidation.ts is AUTHORITATIVE (and the Mac daemon re-validates against
// live git HEAD); this mirror just surfaces obvious errors inline before the admin
// clicks Publish, so they fix them without a server round-trip. Anything this misses,
// the server's 400 + reason backstops (surfaced verbatim in the UI). Keep the RULES in
// sync with publishValidation.ts; the messages here are friendlier but the shape checks
// are the same.

import type { FormDefinition, Section } from "./types";
import { FIELD_INPUTS, RESERVED_KEYS } from "./editorModel";
import type { CatalogParent } from "./registry";

const KEY_RE = /^[a-z0-9_]+$/;
const FORM_CODE_RE = /^[a-z0-9-]+-v[0-9]+$/;
const SLUG_RE = /^[a-z0-9-]+$/;
// Derived from the single source of truth (editorModel.FIELD_INPUTS, which mirrors
// forms/meta-schema.json + types.ts Input). Previously a hand-copied literal, which drifted:
// PR-1 (#271) added "photo" to FIELD_INPUTS + worker/publishValidation but missed THIS copy,
// so every photo field tripped "has an invalid input type." and blocked Publish client-side.
// Deriving here kills that three-copies drift class; the parity test in __tests__ locks it.
const INPUTS = new Set<string>(FIELD_INPUTS);

export interface ValidationContext {
  identity: string;
  parentFormCode: string;
}

/** Catalog-level parent-grouping guard (mirrors apply_publish + the Worker's
 * validateParentGrouping): when CREATING / ADD-VERSIONing a form under an EXISTING form
 * type, a standalone (no-variant) parent can't take a second form, and a variant parent
 * needs a (unique) variant label. Returns a friendly inline error, or null if fine. A
 * brand-new form type is always fine. (This is the rule that caught the "JHA test under
 * jha" case at the daemon; surfacing it here blocks Publish up front.) */
export function checkParentGrouping(
  catalog: CatalogParent[], parentFormCode: string, variantLabel: string | null,
): string | null {
  const p = catalog.find((x) => x.parent_form_code === parentFormCode);
  if (!p) return null;
  if (p.variants.length === 0) {
    return `Form type "${p.name}" already has a standalone form — give this a new form ` +
      `type name, or pick a form type that uses variants.`;
  }
  if (!variantLabel) {
    return `Form type "${p.name}" uses variants — give this form a variant label, or choose a new form type.`;
  }
  if (p.variants.some((v) => v.variant_label === variantLabel)) {
    return `Form type "${p.name}" already has a "${variantLabel}" variant — choose a different label.`;
  }
  return null;
}

/** Return a flat list of human-readable problems. Empty array = clean (client-side). */
export function validateDraft(def: FormDefinition, ctx: ValidationContext): string[] {
  const errors: string[] = [];

  // ── Identity envelope ─────────────────────────────────────────────────────
  if (!ctx.identity || !SLUG_RE.test(ctx.identity)) {
    errors.push("Identity must be lowercase letters, digits, and hyphens (e.g. jha-night).");
  }
  if (!ctx.parentFormCode || !SLUG_RE.test(ctx.parentFormCode)) {
    errors.push("Form type (parent) must be lowercase letters, digits, and hyphens.");
  }

  // ── Top-level definition fields ───────────────────────────────────────────
  if (!Number.isInteger(def.version) || def.version < 1) {
    errors.push("Version must be a whole number ≥ 1.");
  }
  if (!def.form_code || !FORM_CODE_RE.test(def.form_code)) {
    errors.push("Form code must look like identity-v<version> (e.g. jha-v2).");
  } else if (def.form_code !== `${ctx.identity}-v${def.version}`) {
    errors.push(`Form code must be ${ctx.identity}-v${def.version}.`);
  }
  if (def.parent_form_code && def.parent_form_code !== ctx.parentFormCode) {
    errors.push("Form code's parent must match the chosen form type.");
  }
  if (!def.form_name || !def.form_name.trim()) {
    errors.push("Form name is required.");
  }
  if (def.variant_label !== null && def.variant_label !== undefined && !def.variant_label.trim()) {
    errors.push("Variant label is empty — clear it (no variant) or give it a value.");
  }
  if (!Array.isArray(def.sections) || def.sections.length === 0) {
    errors.push("Add at least one section.");
    return errors; // nothing more to check
  }

  // ── Per-section + cross-section uniqueness ────────────────────────────────
  const topLevel: string[] = [];
  def.sections.forEach((s, i) => {
    validateSection(s, i, topLevel, errors);
  });

  const seen = new Set<string>();
  for (const k of topLevel) {
    if (seen.has(k)) errors.push(`Value key "${k}" is used in more than one section — keys must be unique.`);
    seen.add(k);
  }
  return errors;
}

function pushKey(key: string, topLevel: string[], where: string, errors: string[]): void {
  if (RESERVED_KEYS.has(key)) {
    errors.push(`${where}: "${key}" is reserved for the job / work-date envelope — rename it.`);
    return;
  }
  topLevel.push(key);
}

function validateSection(s: Section, idx: number, topLevel: string[], errors: string[]): void {
  const where = `Section ${idx + 1}`;
  switch (s.type) {
    case "header": {
      if (s.fields.length === 0) {
        errors.push(`${where} (header) has no fields.`);
        return;
      }
      const local = new Set<string>();
      for (const f of s.fields) {
        checkField(f, where, errors);
        if (local.has(f.key)) errors.push(`${where}: duplicate field key "${f.key}".`);
        local.add(f.key);
        if (!RESERVED_KEYS.has(f.key)) topLevel.push(f.key);
        else if (f.key === "job" || f.key === "work_date") {
          // job/work_date ARE allowed as header fields (envelope-bound) — no error.
        }
      }
      return;
    }
    case "static_text":
      if (!s.text || !s.text.trim()) errors.push(`${where} (static text) is empty.`);
      return;
    case "repeating_table":
    case "signature_table": {
      checkSectionKey(s.key, where, topLevel, errors);
      if (!s.columns || s.columns.length === 0) {
        errors.push(`${where} (${labelFor(s.type)}) needs at least one column.`);
        return;
      }
      const local = new Set<string>();
      let sigCount = 0;
      for (const c of s.columns) {
        checkField(c, where, errors);
        if (local.has(c.key)) errors.push(`${where}: duplicate column key "${c.key}".`);
        local.add(c.key);
        if (c.input === "signature") sigCount++;
        // Photo fields are header-level only (v1) — table rows hold strings, and the PDF
        // renderer (PR-2) lays photos out as header-level figures, not table cells.
        if (c.input === "photo") errors.push(`${where}: photo fields are header-level only (not table columns).`);
      }
      if (s.type === "signature_table" && sigCount !== 1) {
        errors.push(`${where} (signature table) must have exactly one signature column (has ${sigCount}).`);
      }
      return;
    }
    case "checklist": {
      checkSectionKey(s.key, where, topLevel, errors);
      if (!s.groups || s.groups.length === 0) {
        errors.push(`${where} (checklist) needs at least one group.`);
        return;
      }
      const local = new Set<string>();
      for (const g of s.groups) {
        if (!g.key || !KEY_RE.test(g.key)) errors.push(`${where}: invalid group key "${g.key}".`);
        if (!g.label || !g.label.trim()) errors.push(`${where}: group "${g.key}" needs a label.`);
        if (!g.scale || g.scale.length === 0 || g.scale.some((x) => !x || !x.trim())) {
          errors.push(`${where}: group "${g.key}" needs a non-empty response scale.`);
        }
        if (!g.items || g.items.length === 0) {
          errors.push(`${where}: group "${g.key}" needs at least one item.`);
        }
        if (local.has(g.key)) errors.push(`${where}: duplicate group key "${g.key}".`);
        local.add(g.key);
        for (const it of g.items ?? []) {
          if (!it.key || !KEY_RE.test(it.key)) errors.push(`${where}: invalid item key "${it.key}".`);
          if (!it.label || !it.label.trim()) errors.push(`${where}: item "${it.key}" needs a label.`);
          if (local.has(it.key)) errors.push(`${where}: duplicate key "${it.key}".`);
          local.add(it.key);
        }
      }
      return;
    }
    case "freeform":
      checkSectionKey(s.key, where, topLevel, errors);
      if (!s.label || !s.label.trim()) errors.push(`${where} (free-form) needs a label.`);
      return;
    case "content_blocks":
      checkSectionKey(s.key, where, topLevel, errors);
      if (!s.blocks || s.blocks.length === 0) {
        errors.push(`${where} (content blocks) needs at least one block.`);
        return;
      }
      s.blocks.forEach((b, bi) => {
        if (!b.body || !b.body.trim()) errors.push(`${where}: block ${bi + 1} has no body text.`);
      });
      return;
    // guidance / form_link (SOP daily form, slice D1): read-only in the builder — the
    // definition is authored via the git publish pipeline. Mirror the worker's
    // structural checks only (no catalog lookup client-side; the server backstops the
    // form_link parent-existence rule). Neither contributes a top-level value key.
    case "guidance":
      if (!s.heading || !s.heading.trim()) errors.push(`${where} (guidance) needs a heading.`);
      if (!s.blocks || s.blocks.length === 0) {
        errors.push(`${where} (guidance) needs at least one block.`);
        return;
      }
      s.blocks.forEach((b, bi) => {
        if (b.type === "p" && (!b.text || !b.text.trim())) {
          errors.push(`${where}: guidance paragraph ${bi + 1} is empty.`);
        } else if (b.type === "bullets" && (!b.items || b.items.length === 0 || b.items.some((x) => !x || !x.trim()))) {
          errors.push(`${where}: guidance bullet list ${bi + 1} has an empty item.`);
        } else if (b.type === "callout" && (!b.text || !b.text.trim())) {
          errors.push(`${where}: guidance callout ${bi + 1} is empty.`);
        }
      });
      return;
    case "form_link":
      if (!s.label || !s.label.trim()) errors.push(`${where} (form link) needs a label.`);
      if (!s.parent_form_code || !SLUG_RE.test(s.parent_form_code)) {
        errors.push(`${where} (form link) needs a valid form-type code (lowercase letters, digits, hyphens).`);
      }
      return;
    // job_requirements (slice D4): read-only in the builder like guidance/form_link — the
    // placeholder is authored in the definition via the git publish pipeline. Its key IS a
    // top-level value key (the answers array files there), so it joins the uniqueness check.
    case "job_requirements":
      checkSectionKey(s.key, where, topLevel, errors);
      return;
    // expected_materials (Material receipts M2): read-only in the builder like
    // job_requirements. From daily-report-v7 its key IS value-bearing (the host files the day's
    // expected-materials snapshot there); either way the key occupies the value namespace
    // (mirrors worker/publishValidation.ts), so it joins the uniqueness check.
    case "expected_materials":
      checkSectionKey(s.key, where, topLevel, errors);
      return;
    // additional_photos (DR-photo-pool Slice 1): read-only in the builder, same as the two
    // above. Its key is the FIXED wire key 'additional_photos' and the submission files pool
    // REFERENCES under it, so it very much occupies the value namespace.
    //
    // This case was MISSING. `validateSection` returns void and the project does not set
    // `noImplicitReturns`, so a section type with no case falls out of the switch silently and
    // tsc cannot see the hole. The consequence was narrow but real: an additional_photos key
    // colliding with another section's key passed the in-builder check and was caught only
    // later by the Worker's publish validation — turning an inline editor error into a failed
    // publish. `FormEditor.readonly.test.tsx` now carries an additional_photos fixture so the
    // omission cannot recur unnoticed.
    case "additional_photos":
      checkSectionKey(s.key, where, topLevel, errors);
      return;
  }
}

function labelFor(type: string): string {
  return type.replace(/_/g, " ");
}

function checkSectionKey(key: string, where: string, topLevel: string[], errors: string[]): void {
  if (!key || !KEY_RE.test(key)) {
    errors.push(`${where}: section key "${key}" must be snake_case (lowercase letters, digits, underscores).`);
    return;
  }
  pushKey(key, topLevel, where, errors);
}

function checkField(
  f: { key: string; label: string; input: string; options?: string[] },
  where: string,
  errors: string[],
): void {
  if (!f.key || !KEY_RE.test(f.key)) {
    errors.push(`${where}: field key "${f.key}" must be snake_case (lowercase letters, digits, underscores).`);
  }
  if (!f.label || !f.label.trim()) {
    errors.push(`${where}: field "${f.key}" needs a label.`);
  }
  if (!INPUTS.has(f.input)) {
    errors.push(`${where}: field "${f.key}" has an invalid input type.`);
  }
  if (f.input === "select") {
    const opts = (f.options ?? []).filter((o) => o && o.trim());
    if (opts.length === 0) {
      errors.push(`${where}: select field "${f.key}" needs at least one non-empty option.`);
    }
  }
}
