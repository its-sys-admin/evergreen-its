// Pure, UI-free helpers for the admin Form Editor (Phase-2 slice 3, the B8 sectioned
// builder). The editor composes a FormDefinition from the CLOSED vocabulary
// (forms/meta-schema.json mirrored in types.ts); these factories + transforms keep the
// React component declarative and let the same logic drive create / edit / add-version
// without duplicating the shape rules. NOTHING here sends or fetches — the only mutation
// path off the SPA is the send-free publish enqueue (lib/api.ts).

import type {
  ContentBlock,
  Field,
  FormDefinition,
  Group,
  Input,
  Item,
  Section,
} from "./types";

export const ARCHETYPES = [
  "rows_signatures",
  "grouped_checklist",
  "content_signin",
  "visitor_rows",
  "sectioned_assessment",
] as const;
export type Archetype = (typeof ARCHETYPES)[number];

export const FIELD_INPUTS: Input[] = [
  "text",
  "textarea",
  "date",
  "time",
  "number",
  "select",
  "signature",
  "photo",
];

export const ITEM_KINDS = ["rated", "numeric", "circle_one", "text"] as const;
export type ItemKind = (typeof ITEM_KINDS)[number];

// The BUILDER-COMPOSABLE section types (the "+ add section" buttons). `guidance` and
// `form_link` (SOP daily form, slice D1) are deliberately NOT here: they are authored
// in the form DEFINITION via the git publish pipeline, not composed in the builder —
// the editor renders them read-only (see FormEditor SectionEditor's fallback pane).
// `additional_photos` (the DR-photo-pool mount) IS composable (Photos program,
// 2026-08-27): any form may carry AT MOST ONE pool mount — the fixed wire key is set
// by construction in blankSection, and FormEditor disables the add button once a
// mount exists (server half: worker/publishValidation.ts one-mount + fixed-key rules).
export const SECTION_TYPES = [
  "header",
  "static_text",
  "repeating_table",
  "signature_table",
  "checklist",
  "freeform",
  "content_blocks",
  "additional_photos",
] as const;
export type SectionType = (typeof SECTION_TYPES)[number];

/** Section types authored ONLY in the git-owned form definition (publish pipeline) — the
 *  builder renders them read-only AND suppresses their Remove/Move controls (Slice 1,
 *  R3-F3): the required-content floor now REJECTS a daily-report definition missing its
 *  job_requirements / expected_materials mounts, so the UI must not even offer the
 *  amputation the C3 gates would refuse. Complement of SECTION_TYPES by design — keep the
 *  two in sync when the meta-schema grows a definition-managed type. (`additional_photos`
 *  moved OUT of this set to SECTION_TYPES on 2026-08-27 — the pool became a builder
 *  palette item; it is not part of any required-content floor.) */
export const READ_ONLY_SECTION_TYPES: ReadonlySet<Section["type"]> = new Set([
  "guidance",
  "form_link",
  "job_requirements",
  "expected_materials",
]);

/** Human labels for EVERY section type (the builder picker uses the SECTION_TYPES
 *  subset; the section-list header must label the read-only types too). */
export const SECTION_TYPE_LABELS: Record<Section["type"], string> = {
  header: "Header fields",
  static_text: "Static text",
  repeating_table: "Repeating table",
  signature_table: "Signature table",
  checklist: "Checklist",
  freeform: "Free-form text",
  content_blocks: "Content blocks",
  guidance: "SOP guidance (read-only)",
  form_link: "Form link (read-only)",
  job_requirements: "Per-job requirements (read-only)",
  expected_materials: "Expected materials (read-only)",
  additional_photos: "Additional photos (pool)",
};

// The validator's KEY_RE: every field/section/group/item key is snake_case lowercase.
const KEY_RE = /^[a-z0-9_]+$/;
const SLUG_RE = /^[a-z0-9-]+$/;

/** Lowercase a free-text label into a snake_case key candidate ([a-z0-9_]). */
export function slugifyKey(label: string): string {
  return label
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 60);
}

/** Lowercase a free-text label into a hyphen identity slug ([a-z0-9-]). */
export function slugifyIdentity(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80);
}

export function isValidKey(k: string): boolean {
  return KEY_RE.test(k);
}
export function isValidIdentity(k: string): boolean {
  return SLUG_RE.test(k);
}

/** A unique key in `existing`, derived from `base` (or a fallback prefix), suffixing
 *  _2, _3, … on collision. Used when adding fields/items/sections so the editor never
 *  emits a duplicate key (which the validator rejects). */
export function uniqueKey(base: string, existing: Set<string>, fallback = "key"): string {
  let candidate = slugifyKey(base) || fallback;
  if (!existing.has(candidate)) return candidate;
  let n = 2;
  while (existing.has(`${candidate}_${n}`)) n++;
  return `${candidate}_${n}`;
}

// ── Blank factories for each vocabulary element ─────────────────────────────────

export function blankField(key: string, input: Input = "text"): Field {
  const f: Field = { key, label: "", input };
  if (input === "select") f.options = [""];
  return f;
}

export function blankItem(key: string): Item {
  return { key, label: "" };
}

export function blankGroup(key: string): Group {
  return { key, label: "", scale: ["OK", "NOT OK", "N/A"], items: [blankItem(`${key}_item`)] };
}

export function blankBlock(): ContentBlock {
  // Omit heading (it's optional): an empty-string heading is rejected by the server
  // validator (isStr("") is false) but the heading input is optional in the builder, so
  // a default "" would compose a payload the server ALWAYS rejects. The heading onChange
  // already maps "" → undefined; this keeps an untouched block valid too.
  return { body: "" };
}

/** A blank section of the requested type, with one starter child where the schema
 *  requires a non-empty collection (columns / groups / blocks). */
export function blankSection(type: SectionType): Section {
  switch (type) {
    case "header":
      return { type: "header", fields: [blankField("field_1")] };
    case "static_text":
      return { type: "static_text", text: "", emphasis: "heading" };
    case "repeating_table":
      return {
        type: "repeating_table",
        key: "table",
        columns: [blankField("col_1")],
        min_rows: 1,
        allow_add: true,
      };
    case "signature_table":
      return {
        type: "signature_table",
        key: "sign_in",
        columns: [blankField("name"), blankField("signature", "signature")],
        min_rows: 1,
        allow_add: true,
      };
    case "checklist":
      return { type: "checklist", key: "checklist", groups: [blankGroup("group_1")] };
    case "freeform":
      return { type: "freeform", key: "notes", label: "", input: "textarea" };
    case "content_blocks":
      return { type: "content_blocks", key: "content", blocks: [blankBlock()] };
    case "additional_photos":
      // The key is the FIXED wire key by construction — /api/submit validates + claims
      // exactly values.additional_photos, and publishValidation rejects any other key.
      // The editor never renders a key input for this section (FormEditor's pool case).
      return { type: "additional_photos", key: "additional_photos", title: "Additional photos" };
  }
}

/**
 * The "+ Photos" builder MACRO (Photos program, 2026-08-27): not a section type of its
 * own — it composes a plain header section carrying ONE optional inline photo field
 * (shape precedented by material-incident-v1), so it needs zero meta-schema / renderer
 * ripple. No `required`, no `max_count` (default 4); the key is uniquified against the
 * draft's existing top-level keys so a second insertion suffixes photos_2, photos_3, ….
 */
export function photosMacroSection(existingTopLevelKeys: Set<string>): Section {
  return {
    type: "header",
    title: "Photos",
    fields: [{ key: uniqueKey("photos", existingTopLevelKeys), label: "Photos", input: "photo" }],
  };
}

/** A brand-new blank FormDefinition for the create flow. The identity / parent are set
 *  by the editor's identity panel; version is always 1 for a new identity. */
export function blankDefinition(): FormDefinition {
  return {
    form_code: "",
    parent_form_code: "",
    form_name: "",
    variant_label: null,
    version: 1,
    archetype: "sectioned_assessment",
    source_pdf: "",
    sections: [blankSection("header")],
  };
}

/** Deep clone via JSON (definitions are plain JSON — no functions / dates). */
export function cloneDefinition(def: FormDefinition): FormDefinition {
  return JSON.parse(JSON.stringify(def)) as FormDefinition;
}

/** Recompute form_code from identity + version (the validator's invariant). */
export function formCodeFor(identity: string, version: number): string {
  return `${identity}-v${version}`;
}

/**
 * EDIT transform: keep the same identity, bump the version (jha-v1 → jha-v2), and
 * recompute form_code. The caller supplies the prior version so the bump is N+1.
 */
export function toEditDraft(def: FormDefinition, identity: string): FormDefinition {
  const next = cloneDefinition(def);
  next.version = def.version + 1;
  next.form_code = formCodeFor(identity, next.version);
  next.parent_form_code = def.parent_form_code;
  return next;
}

/**
 * ADD-VERSION / clone transform: a brand-new identity (manual slug), version 1, cloning
 * the source form's sections + archetype + parent. The new identity + name are filled in
 * by the editor (we blank the name so the admin must title it). source_pdf is carried
 * (optional) but blanked — editor-authored forms don't need a reference PDF.
 */
export function toClonedDraft(
  def: FormDefinition,
  newIdentity: string,
  parentFormCode: string,
): FormDefinition {
  const next = cloneDefinition(def);
  next.version = 1;
  next.parent_form_code = parentFormCode;
  next.form_code = formCodeFor(newIdentity, 1);
  next.form_name = def.form_name; // pre-fill; admin renames
  next.source_pdf = "";
  return next;
}

/** All top-level value keys a definition contributes (header non-reserved fields +
 *  keyed section keys) — used for cross-section-unique-key checks AND duplicate guards
 *  when generating new keys. Mirrors the validator's `topLevel` accumulation. */
const RESERVED_KEYS = new Set(["job", "work_date"]);
export function topLevelKeys(def: FormDefinition): string[] {
  const out: string[] = [];
  for (const s of def.sections) {
    if (s.type === "header") {
      for (const f of s.fields) if (!RESERVED_KEYS.has(f.key)) out.push(f.key);
    } else if (s.type !== "static_text" && s.type !== "guidance" && s.type !== "form_link") {
      // static_text / guidance / form_link are keyless (no value contribution).
      out.push(s.key);
    }
  }
  return out;
}

export { RESERVED_KEYS };
