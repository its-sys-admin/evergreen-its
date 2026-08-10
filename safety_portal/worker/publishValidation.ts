import catalogManifest from "../catalog.json";
import requiredContent from "../required-content.json";
import workflows from "../workflows.json";

// Server-side validation of a composed form definition at the POST /api/admin/publish
// enqueue gate (design brief C3). The Worker is the FIRST gate; the Mac daemon (slice
// 3b) RE-validates against the live git HEAD (authoritative) and CI runs the 3-renderer
// smoke (3c). ajv can't run in Cloudflare Workers (the runtime forbids eval / new
// Function, which ajv's runtime schema compilation needs), so this is a hand-rolled
// validator of the CLOSED vocabulary (forms/meta-schema.json + src/forms/types.ts) plus
// the rules a JSON-schema can't express: the reserved-key denylist, cross-section-unique
// value keys, and hard bounds on adversarial input. It returns a STABLE reason string so
// the editor can surface it; it never throws.

const INPUTS = new Set(["text", "textarea", "date", "time", "number", "select", "signature", "photo"]);
// Photo bounds (PR-1, 2026-06-12) — mirror src/components/PhotoField.tsx + worker/index.ts.
const PHOTO_MAX_COUNT = 4;
const ITEM_KINDS = new Set(["rated", "numeric", "circle_one", "text"]);
const FREEFORM_INPUTS = new Set(["textarea", "text"]);
const EMPHASES = new Set(["footer", "heading", "legal"]);
const ARCHETYPES = new Set([
  "rows_signatures", "grouped_checklist", "content_signin", "visitor_rows", "sectioned_assessment",
]);
// The submission envelope (job dropdown + work-date picker) owns these top-level keys;
// the SPA FormRenderer skips header fields named these (ENVELOPE_KEYS). They are allowed
// ONLY as header fields (the existing convention) — never as a non-header value key,
// which would collide with the envelope in the submission's top-level namespace.
const RESERVED_KEYS = new Set(["job", "work_date"]);

const KEY_RE = /^[a-z0-9_]+$/; // every field/section key in the shipped forms is snake_case
const FORM_CODE_RE = /^[a-z0-9-]+-v[0-9]+$/;
const SLUG_RE = /^[a-z0-9-]+$/;

// guidance sections (SOP daily form, slice D1): read-only rich text — the closed block
// vocabulary and callout styles. Plain text only; no HTML block type exists on purpose.
const GUIDANCE_BLOCK_TYPES = new Set(["p", "bullets", "callout"]);
const CALLOUT_STYLES = new Set(["critical", "quality", "note"]);
// form_link parent targets must exist in the DEPLOYED catalog manifest (bundled at build
// time, same vintage as the `catalog` the publish endpoint checks grouping against). The
// repo-side twin is tests/test_form_definitions.py's form_link-parent check vs live HEAD.
const KNOWN_PARENT_FORM_CODES: ReadonlySet<string> = new Set(
  (catalogManifest as { parents: { parent_form_code: string }[] }).parents.map(
    (p) => p.parent_form_code,
  ),
);

// Hard bounds — bound adversarial input; generous vs the real forms (the telehandler
// checklist has 64 items; HSS&E has 8 sections; the SOP daily form — guidance ⊕ fields
// interleaved — has 42 sections, which forced the 40 → 60 bump).
const MAX_SECTIONS = 60;
const MAX_FIELDS = 250;
const MAX_GROUPS = 40;
const MAX_ITEMS = 250;
const MAX_BLOCKS = 100;
const MAX_STR = 8000;

export interface DefinitionContext {
  identity: string;
  parentFormCode: string;
}
export type ValidationResult = { ok: true } | { ok: false; reason: string };

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}
function isStr(v: unknown, max = MAX_STR): v is string {
  return typeof v === "string" && v.length > 0 && v.length <= max;
}
function fail(reason: string): ValidationResult {
  return { ok: false, reason };
}

// The valid workflow id set (form-builder workflow selector / catalog parent `category`) —
// single-sourced in safety_portal/workflows.json, mirrored by shared/form_category.py. Adding
// a workflow there updates BOTH runtimes' validation.
const WORKFLOW_IDS: ReadonlySet<string> = new Set(
  (workflows as { workflows: { id: string }[] }).workflows.map((w) => w.id),
);

/** Validate a workflow category against the registry — the POST /api/admin/publish gate for
 *  create + recategorize. Mirrors shared.form_category.is_valid_category.
 *  A workflow id is short (`^[a-z0-9-]+$`, workflows.schema.json); we cap length so a
 *  hostile/oversized `category` can't be reflected back in the 400 body, and keep the failure
 *  reason a STATIC string (never echo caller input back into the response). */
const MAX_CATEGORY_LEN = 64;

export function validateCategory(value: unknown): ValidationResult {
  if (typeof value !== "string" || value.length > MAX_CATEGORY_LEN || !WORKFLOW_IDS.has(value)) {
    return fail("unknown workflow category");
  }
  return { ok: true };
}

// ── Required-content legal floor (Brief 1 PR-1) ──────────────────────────────────────
// validateDefinition checks STRUCTURE; required-content.json adds the per-identity LEGAL
// FLOOR — a JHA must keep its "REVIEW AND REVISE THE PLAN" footer, an equipment form its
// lock/tag-out line, most forms a signature mechanism. Enforced HERE (the enqueue gate) AND
// in safety_reports/publish_manifest.apply_publish (the daemon's authoritative re-check vs
// live HEAD, C3). Reason strings start "required content missing:" so the editor's
// explainPublish surfaces them verbatim. Effective spec = parents[parent] merged with
// identities[identity] (identity wins); if NEITHER exists, defaults_for_new_identities.
interface RequiredSpec {
  required_section_types?: string[];
  required_signature_inputs_min?: number;
  required_static_text?: string[];
  required_field_keys?: string[];
}
const REQUIRED_CONTENT = requiredContent as unknown as {
  defaults_for_new_identities?: RequiredSpec;
  parents?: Record<string, RequiredSpec>;
  identities?: Record<string, RequiredSpec>;
};

function requiredSpecFor(identity: string, parentFormCode: string): RequiredSpec {
  const parentSpec = REQUIRED_CONTENT.parents?.[parentFormCode];
  const identitySpec = REQUIRED_CONTENT.identities?.[identity];
  if (parentSpec === undefined && identitySpec === undefined) {
    return REQUIRED_CONTENT.defaults_for_new_identities ?? {};
  }
  return { ...(parentSpec ?? {}), ...(identitySpec ?? {}) };
}

/** Every field/column object across a definition's sections (header fields + table columns). */
function allFieldObjects(def: Record<string, unknown>): Record<string, unknown>[] {
  const out: Record<string, unknown>[] = [];
  for (const s of (def.sections as unknown[]) ?? []) {
    if (!isObject(s)) continue;
    for (const f of (s.fields as unknown[]) ?? []) if (isObject(f)) out.push(f);
    for (const c of (s.columns as unknown[]) ?? []) if (isObject(c)) out.push(c);
  }
  return out;
}

/** The legal floor: a create/edit/add_version definition must satisfy its required-content
 *  spec. Structure is already validated by validateSection before this runs. */
function validateRequiredContent(def: Record<string, unknown>, ctx: DefinitionContext): ValidationResult {
  const spec = requiredSpecFor(ctx.identity, ctx.parentFormCode);
  const sections = ((def.sections as unknown[]) ?? []).filter(isObject);
  const types = new Set(sections.map((s) => s.type));
  for (const t of spec.required_section_types ?? []) {
    if (!types.has(t)) return fail(`required content missing: ${ctx.identity} must contain a '${t}' section`);
  }
  const sigCount = allFieldObjects(def).filter((f) => f.input === "signature").length;
  const minSigs = spec.required_signature_inputs_min ?? 0;
  if (sigCount < minSigs) {
    return fail(`required content missing: ${ctx.identity} needs at least ${minSigs} signature input(s)`);
  }
  const legalTexts = sections
    .filter((s) => s.type === "static_text" && (s.emphasis === "legal" || s.emphasis === "footer"))
    .map((s) => String(s.text ?? ""));
  for (const required of spec.required_static_text ?? []) {
    if (!legalTexts.some((t) => t.includes(required))) {
      return fail(`required content missing: the mandatory legal/footer line "${required}" is absent from ${ctx.identity}`);
    }
  }
  if ((spec.required_field_keys ?? []).length > 0) {
    const keys = new Set<string>();
    for (const f of allFieldObjects(def)) if (typeof f.key === "string") keys.add(f.key);
    for (const s of sections) {
      if (typeof s.key === "string") keys.add(s.key);
      for (const g of (s.groups as unknown[]) ?? []) {
        if (!isObject(g)) continue;
        if (typeof g.key === "string") keys.add(g.key);
        for (const it of (g.items as unknown[]) ?? []) {
          if (isObject(it) && typeof it.key === "string") keys.add(it.key);
        }
      }
    }
    for (const k of spec.required_field_keys ?? []) {
      if (!keys.has(k)) return fail(`required content missing: core field '${k}' absent from ${ctx.identity}`);
    }
  }
  return { ok: true };
}

/** A header/table column field: { key, label, input, options?, required? }. */
function validateField(f: unknown, where: string): { key: string } | string {
  if (!isObject(f)) return `${where}: field is not an object`;
  if (!isStr(f.key) || !KEY_RE.test(f.key as string)) return `${where}: invalid field key`;
  if (!isStr(f.label)) return `${where}: field ${f.key as string} missing label`;
  if (typeof f.input !== "string" || !INPUTS.has(f.input)) return `${where}: field ${f.key as string} invalid input`;
  if (f.options !== undefined) {
    if (!Array.isArray(f.options) || !f.options.every((o) => isStr(o, 500))) {
      return `${where}: field ${f.key as string} invalid options`;
    }
  }
  if (f.input === "select" && (!Array.isArray(f.options) || f.options.length === 0)) {
    return `${where}: select field ${f.key as string} needs non-empty options`;
  }
  if (f.required !== undefined && typeof f.required !== "boolean") return `${where}: field ${f.key as string} invalid required`;
  if (f.max_count !== undefined) {
    if (
      f.input !== "photo" || typeof f.max_count !== "number" ||
      !Number.isInteger(f.max_count) || f.max_count < 1 || f.max_count > PHOTO_MAX_COUNT
    ) {
      return `${where}: field ${f.key as string} invalid max_count (photo fields only, 1..${PHOTO_MAX_COUNT})`;
    }
  }
  return { key: f.key as string };
}

/**
 * Validate ONE section, accumulating its top-level value keys into `topLevel` (header
 * non-reserved fields + the keyed section's own key) so the caller can enforce global
 * uniqueness + the reserved-key rule across the whole form.
 */
function validateSection(s: unknown, idx: number, topLevel: string[]): string | null {
  const where = `section[${idx}]`;
  if (!isObject(s)) return `${where} is not an object`;
  const type = s.type;
  if (typeof type !== "string") return `${where} missing type`;

  const keyedSectionKey = (): string | null => {
    if (!isStr(s.key) || !KEY_RE.test(s.key as string)) return `${where}: invalid section key`;
    if (RESERVED_KEYS.has(s.key as string)) return `${where}: '${s.key as string}' is reserved for the submission envelope`;
    topLevel.push(s.key as string);
    return null;
  };
  const localUnique = (keys: string[], label: string): string | null => {
    const seen = new Set<string>();
    for (const k of keys) {
      if (seen.has(k)) return `${where}: duplicate ${label} key '${k}'`;
      seen.add(k);
    }
    return null;
  };

  switch (type) {
    case "header": {
      if (!Array.isArray(s.fields) || s.fields.length > MAX_FIELDS) return `${where}: invalid header fields`;
      const keys: string[] = [];
      for (const f of s.fields) {
        const r = validateField(f, where);
        if (typeof r === "string") return r;
        keys.push(r.key);
        // job/work_date header fields are envelope-skipped → NOT top-level value keys.
        if (!RESERVED_KEYS.has(r.key)) topLevel.push(r.key);
      }
      return localUnique(keys, "header field");
    }
    case "static_text": {
      if (!isStr(s.text)) return `${where}: static_text missing text`;
      if (s.emphasis !== undefined && (typeof s.emphasis !== "string" || !EMPHASES.has(s.emphasis))) {
        return `${where}: invalid emphasis`;
      }
      return null;
    }
    case "repeating_table":
    case "signature_table": {
      const e = keyedSectionKey();
      if (e) return e;
      if (!Array.isArray(s.columns) || s.columns.length === 0 || s.columns.length > MAX_FIELDS) {
        return `${where}: invalid columns`;
      }
      const colKeys: string[] = [];
      let sigCount = 0;
      for (const col of s.columns) {
        const r = validateField(col, where);
        if (typeof r === "string") return r;
        colKeys.push(r.key);
        if (isObject(col) && col.input === "signature") sigCount++;
        // Header-level only (v1): table rows are Record<string,string> on the wire and the
        // Python renderer (PR-2) lays photos out as figures, not cells.
        if (isObject(col) && col.input === "photo") return `${where}: photo fields are header-level only`;
      }
      if (type === "signature_table" && sigCount !== 1) return `${where}: signature_table needs exactly one signature column`;
      if (s.min_rows !== undefined && (typeof s.min_rows !== "number" || !Number.isInteger(s.min_rows) || s.min_rows < 0)) {
        return `${where}: invalid min_rows`;
      }
      if (s.allow_add !== undefined && typeof s.allow_add !== "boolean") return `${where}: invalid allow_add`;
      return localUnique(colKeys, "column");
    }
    case "checklist": {
      const e = keyedSectionKey();
      if (e) return e;
      if (!Array.isArray(s.groups) || s.groups.length === 0 || s.groups.length > MAX_GROUPS) {
        return `${where}: invalid checklist groups`;
      }
      const allKeys: string[] = [];
      for (const g of s.groups) {
        if (!isObject(g)) return `${where}: group is not an object`;
        if (!isStr(g.key) || !KEY_RE.test(g.key as string)) return `${where}: invalid group key`;
        if (!isStr(g.label)) return `${where}: group ${g.key as string} missing label`;
        if (!Array.isArray(g.scale) || g.scale.length === 0 || !g.scale.every((x) => isStr(x, 200))) {
          return `${where}: group ${g.key as string} invalid scale`;
        }
        if (!Array.isArray(g.items) || g.items.length === 0 || g.items.length > MAX_ITEMS) {
          return `${where}: group ${g.key as string} invalid items`;
        }
        allKeys.push(g.key as string);
        for (const it of g.items) {
          if (!isObject(it)) return `${where}: item is not an object`;
          if (!isStr(it.key) || !KEY_RE.test(it.key as string)) return `${where}: invalid item key`;
          if (!isStr(it.label)) return `${where}: item ${it.key as string} missing label`;
          if (it.kind !== undefined && (typeof it.kind !== "string" || !ITEM_KINDS.has(it.kind))) {
            return `${where}: item ${it.key as string} invalid kind`;
          }
          allKeys.push(it.key as string);
        }
      }
      return localUnique(allKeys, "checklist group/item");
    }
    case "freeform": {
      const e = keyedSectionKey();
      if (e) return e;
      if (!isStr(s.label)) return `${where}: freeform missing label`;
      if (s.input !== undefined && (typeof s.input !== "string" || !FREEFORM_INPUTS.has(s.input))) {
        return `${where}: freeform invalid input`;
      }
      return null;
    }
    case "content_blocks": {
      const e = keyedSectionKey();
      if (e) return e;
      if (!Array.isArray(s.blocks) || s.blocks.length === 0 || s.blocks.length > MAX_BLOCKS) {
        return `${where}: invalid content blocks`;
      }
      for (const b of s.blocks) {
        if (!isObject(b)) return `${where}: block is not an object`;
        if (!isStr(b.body)) return `${where}: block missing body`;
        if (b.heading !== undefined && !isStr(b.heading)) return `${where}: block invalid heading`;
      }
      return null;
    }
    // Read-only SOP guidance (slice D1): heading + p/bullets/callout blocks, all plain
    // strings (no HTML vocabulary exists). Contributes NO top-level value keys.
    case "guidance": {
      if (!isStr(s.heading)) return `${where}: guidance missing heading`;
      if (!Array.isArray(s.blocks) || s.blocks.length === 0 || s.blocks.length > MAX_BLOCKS) {
        return `${where}: invalid guidance blocks`;
      }
      for (const b of s.blocks) {
        if (!isObject(b)) return `${where}: guidance block is not an object`;
        if (typeof b.type !== "string" || !GUIDANCE_BLOCK_TYPES.has(b.type)) {
          return `${where}: unknown guidance block type`;
        }
        switch (b.type) {
          case "p":
            if (!isStr(b.text)) return `${where}: guidance paragraph missing text`;
            break;
          case "bullets":
            // Bounded like every sibling repeatable array (security review: an unbounded item list
            // is an authenticated-admin resource-exhaustion vector through publish_requests).
            if (!Array.isArray(b.items) || b.items.length === 0 || b.items.length > MAX_ITEMS || !b.items.every((x) => isStr(x))) {
              return `${where}: guidance bullets need 1-${MAX_ITEMS} non-empty string items`;
            }
            break;
          case "callout":
            if (typeof b.style !== "string" || !CALLOUT_STYLES.has(b.style)) {
              return `${where}: guidance callout invalid style`;
            }
            if (!isStr(b.text)) return `${where}: guidance callout missing text`;
            break;
        }
      }
      return null;
    }
    // Deep link to another form type (slice D1): label + a parent_form_code that must
    // exist in the deployed catalog. Contributes NO top-level value keys.
    case "form_link": {
      if (!isStr(s.label)) return `${where}: form_link missing label`;
      if (!isStr(s.parent_form_code) || !SLUG_RE.test(s.parent_form_code)) {
        return `${where}: form_link invalid parent_form_code`;
      }
      if (!KNOWN_PARENT_FORM_CODES.has(s.parent_form_code)) {
        return `${where}: form_link parent_form_code is not a known form type`;
      }
      if (s.helper !== undefined && !isStr(s.helper)) return `${where}: form_link invalid helper`;
      return null;
    }
    // Per-job daily-form requirements placeholder (slice D4): a keyed mount point with NO
    // content of its own — the D1 overlay (job_daily_requirements) is fetched at render time
    // and the answers file under values.<key>. The key IS a top-level value key (the answers
    // array lands there), so it goes through keyedSectionKey like every value-bearing section.
    // AT MOST ONE per definition — enforced in validateDefinition (a per-section check can't
    // count); documented in forms/meta-schema.json.
    case "job_requirements": {
      const e = keyedSectionKey();
      if (e) return e;
      if (s.title !== undefined && !isStr(s.title)) return `${where}: job_requirements invalid title`;
      return null;
    }
    // Expected-materials mount (Material receipts M2): a keyed mount with no content of its own
    // in the DEFINITION — the Daily tab renders the job's expected materials (D1
    // job_expected_materials, M1) here. From daily-report-v7 the key is VALUE-BEARING: the host
    // files values.<key> = the day's self-describing expected-materials snapshot, which the PDF
    // prints. (v5/v6 filed nothing under it; the namespace was reserved through keyedSectionKey
    // precisely so a later value-bearing section of the same name could not collide with or
    // shadow the mount — which is exactly what arrived, by design, at v7.) The validation is the
    // same either way: a keyed section's key must be unique, whether or not it carries values.
    // AT MOST ONE per definition — enforced in validateDefinition, same discipline as
    // job_requirements; documented in forms/meta-schema.json.
    case "expected_materials": {
      const e = keyedSectionKey();
      if (e) return e;
      if (s.title !== undefined && !isStr(s.title)) return `${where}: expected_materials invalid title`;
      return null;
    }
    // Additional-photos pool mount (DR-photo-pool Slice 1): a keyed mount point with NO content
    // of its own — the Daily tab renders the pool upload/status UI here and the submission files
    // values.<key> = [{pool_id, caption?}] POOL REFERENCES (never bytes — the inline photo field
    // is payload-budgeted; see fieldops_daily_photos.ts). The key MUST be the fixed wire key
    // 'additional_photos': /api/submit validates + claims that exact top-level key (a definition
    // mounting the section under another key would file refs the Worker never claims — silently
    // unclaimed pool rows). AT MOST ONE per definition — enforced in validateDefinition;
    // documented in forms/meta-schema.json.
    case "additional_photos": {
      const e = keyedSectionKey();
      if (e) return e;
      if (s.key !== "additional_photos") {
        return `${where}: additional_photos key must be 'additional_photos' (the /api/submit claim key)`;
      }
      if (s.title !== undefined && !isStr(s.title)) return `${where}: additional_photos invalid title`;
      return null;
    }
    default:
      return `${where}: unknown section type '${type}'`;
  }
}

/**
 * Validate a composed FormDefinition for the create / edit / add_version publish ops.
 * `ctx.identity` + `ctx.parentFormCode` come from the request envelope; the definition
 * must agree with them (form_code = identity-v<version>, parent matches).
 */
export function validateDefinition(def: unknown, ctx: DefinitionContext): ValidationResult {
  if (!isObject(def)) return fail("definition is not an object");

  if (typeof def.version !== "number" || !Number.isInteger(def.version) || def.version < 1) {
    return fail("invalid version");
  }
  if (!isStr(def.form_code) || !FORM_CODE_RE.test(def.form_code)) return fail("invalid form_code");
  if (def.form_code !== `${ctx.identity}-v${def.version}`) {
    return fail(`form_code must be ${ctx.identity}-v${def.version}`);
  }
  if (!isStr(def.parent_form_code) || !SLUG_RE.test(def.parent_form_code)) return fail("invalid parent_form_code");
  if (def.parent_form_code !== ctx.parentFormCode) return fail("parent_form_code does not match the request");
  if (!isStr(def.form_name)) return fail("invalid form_name");
  if (typeof def.archetype !== "string" || !ARCHETYPES.has(def.archetype)) return fail("invalid archetype");
  if (def.variant_label !== undefined && def.variant_label !== null && !isStr(def.variant_label)) {
    return fail("invalid variant_label");
  }
  // source_pdf is OPTIONAL for editor-authored forms (design B9); when present it must
  // be a plain string.
  if (def.source_pdf !== undefined && typeof def.source_pdf !== "string") return fail("invalid source_pdf");

  if (!Array.isArray(def.sections) || def.sections.length === 0 || def.sections.length > MAX_SECTIONS) {
    return fail("invalid sections");
  }
  const topLevel: string[] = [];
  for (let i = 0; i < def.sections.length; i++) {
    const e = validateSection(def.sections[i], i, topLevel);
    if (e) return fail(e);
  }
  // Cross-section-unique top-level value keys (header non-reserved fields + keyed
  // section keys) — a collision would make the submission ambiguous.
  const seen = new Set<string>();
  for (const k of topLevel) {
    if (seen.has(k)) return fail(`duplicate value key '${k}' across sections`);
    seen.add(k);
  }
  // AT MOST ONE job_requirements section (slice D4): the per-job overlay has one mount point —
  // two would double-render the same fetched items and double-file the answers. (The unique-key
  // check above can't catch this: two mounts could carry different keys.)
  const reqMounts = def.sections.filter((s) => isObject(s) && s.type === "job_requirements").length;
  if (reqMounts > 1) return fail("multiple job_requirements sections (at most one)");
  // AT MOST ONE expected_materials section (Material receipts M2): one receipt mount — two would
  // double-render the same fetched rows and double-append deliveries_received rows on confirm.
  const emMounts = def.sections.filter((s) => isObject(s) && s.type === "expected_materials").length;
  if (emMounts > 1) return fail("multiple expected_materials sections (at most one)");
  // AT MOST ONE additional_photos section (DR-photo-pool): one pool mount — two would double-
  // render the same refs list. (The fixed-key rule above means the cross-section-unique key check
  // ALSO catches a double mount, but only via a key-collision message; this names the real rule.)
  const apMounts = def.sections.filter((s) => isObject(s) && s.type === "additional_photos").length;
  if (apMounts > 1) return fail("multiple additional_photos sections (at most one)");
  // values.additional_photos is WIRE-RESERVED (the /api/submit pool-claim parses that exact
  // top-level key on EVERY form): only the additional_photos mount may own it. Any other
  // section/field claiming the key would make every submission of the form 400
  // (invalid_additional_photos/refs_not_array) — reject at publish instead.
  if (apMounts === 0 && topLevel.includes("additional_photos")) {
    return fail("'additional_photos' is reserved for the additional_photos section");
  }
  // Legal-floor re-check (Brief 1 PR-1) — structure is valid above; now require the
  // per-identity mandatory content (signature mechanism, legal/footer lines, core fields).
  const rc = validateRequiredContent(def, ctx);
  if (!rc.ok) return rc;
  return { ok: true };
}

// ── catalog-level parent grouping (mirrors apply_publish's variant-mixing rule) ──────
// A "form type" (parent) is EITHER one standalone (no-variant) form OR a set of named
// variants — never a mix. validateDefinition checks ONE definition in isolation and can't
// see this; checked here against the DEPLOYED manifest (== live, since the daemon redeploys
// after each publish). The daemon re-checks against live git HEAD authoritatively (C3) —
// this is the enqueue-time guard so a doomed create/add-version is rejected with a clear
// reason instead of queuing + failing at the daemon.

interface ManifestForm {
  variant_label: string | null;
  status: string;
}
interface ManifestParent {
  parent_form_code: string;
  forms: ManifestForm[];
}
export interface CatalogManifest {
  parents: ManifestParent[];
}

/** For a create / add_version, reject if adding the form to its parent would mix a
 * standalone (null-variant) form with variant forms, or collide on a variant label. A
 * brand-new parent is always fine (it becomes a new form type). */
export function validateParentGrouping(
  manifest: CatalogManifest,
  parentFormCode: string,
  variantLabel: string | null | undefined,
): ValidationResult {
  const parent = manifest.parents.find((p) => p.parent_form_code === parentFormCode);
  if (!parent) return { ok: true };
  const active = parent.forms.filter((f) => f.status === "active");
  if (active.length === 0) return { ok: true };
  if (active.some((f) => f.variant_label === null)) {
    return fail(
      `form type '${parentFormCode}' already has a standalone form — give this a new form ` +
        `type, or pick a form type that uses variants`,
    );
  }
  const label = variantLabel ?? null;
  if (label === null) {
    return fail(
      `form type '${parentFormCode}' uses variants — give this form a variant label, or use a new form type`,
    );
  }
  if (active.some((f) => f.variant_label === label)) {
    return fail(`form type '${parentFormCode}' already has a '${label}' variant — choose a different label`);
  }
  return { ok: true };
}
