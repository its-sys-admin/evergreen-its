import type {
  ContentBlock,
  Field,
  FormDefinition,
  Group,
  Input,
  Item,
  Section,
} from "../forms/types";
import {
  ARCHETYPES,
  FIELD_INPUTS,
  ITEM_KINDS,
  READ_ONLY_SECTION_TYPES,
  SECTION_TYPES,
  SECTION_TYPE_LABELS,
  blankBlock,
  blankField,
  blankGroup,
  blankItem,
  blankSection,
  slugifyKey,
  type SectionType,
} from "../forms/editorModel";
import { WORKFLOWS_ORDERED } from "../forms/registry";

/**
 * The B8 sectioned form builder (Phase-2 slice 3). A controlled editor over a
 * FormDefinition: an identity panel (form_code is DERIVED, not typed) + an ordered list
 * of section editors, each surfacing ONLY the closed vocabulary
 * (forms/meta-schema.json). It mutates the draft through one `onChange(next)` callback so
 * the parent owns the single source of truth and can drive the live FormRenderer preview
 * + client validation off the same object. It never sends or fetches — publish is the
 * parent's send-free enqueue.
 *
 * Accessibility: every control has a <label> or aria-label; add/remove/move are real
 * <button>s; the section list is keyboard-navigable.
 */

interface Props {
  def: FormDefinition;
  onChange: (next: FormDefinition) => void;
  /** create / edit / add_version drive which identity fields are editable. */
  mode: "create" | "edit" | "add_version";
  /** The derived identity (parent-vN stripped); read-only in edit mode. */
  identity: string;
  onIdentityChange: (identity: string) => void;
  parentFormCode: string;
  onParentChange: (parent: string) => void;
  /** Known parent_form_codes (for the datalist), so a new identity can join an existing
   *  form type without typos. */
  knownParents: string[];
  /** The form type's workflow (workflows.json id). Editable on CREATE (sets a new parent's
   *  workflow); read-only on edit/add_version (a form type's workflow is changed from its card). */
  category: string;
  onCategoryChange: (category: string) => void;
}

export function FormEditor(props: Props) {
  const { def, onChange } = props;

  // Section list mutators — all go through a single immutable replace.
  const setSections = (sections: Section[]) => onChange({ ...def, sections });
  const updateSection = (idx: number, next: Section) =>
    setSections(def.sections.map((s, i) => (i === idx ? next : s)));
  const addSection = (type: SectionType) => setSections([...def.sections, blankSection(type)]);
  const removeSection = (idx: number) => setSections(def.sections.filter((_, i) => i !== idx));
  const moveSection = (idx: number, dir: -1 | 1) => {
    const j = idx + dir;
    if (j < 0 || j >= def.sections.length) return;
    const copy = [...def.sections];
    [copy[idx], copy[j]] = [copy[j], copy[idx]];
    setSections(copy);
  };

  return (
    <div>
      <IdentityPanel {...props} />

      <h3 className="form-editor__sections-heading">Sections</h3>
      <ol className="form-editor__section-list">
        {def.sections.map((s, i) => (
          <li key={i} className="form-editor__section card">
            <div className="form-editor__section-head">
              <span className="form-editor__section-type">{SECTION_TYPE_LABELS[s.type]}</span>
              {/* Definition-managed (read-only) sections get NO Remove/Move controls (Slice 1,
                  R3-F3): the required-content floor rejects a daily-report definition missing
                  its job_requirements / expected_materials mounts, so the builder must not
                  even offer the amputation the C3 gates would refuse. */}
              {READ_ONLY_SECTION_TYPES.has(s.type) ? (
                <span className="muted">definition-managed</span>
              ) : (
                <div className="form-editor__section-controls">
                  <button
                    type="button"
                    className="btn btn--secondary form-editor__icon-btn"
                    aria-label={`Move section ${i + 1} up`}
                    disabled={i === 0}
                    onClick={() => moveSection(i, -1)}
                  >
                    ↑
                  </button>
                  <button
                    type="button"
                    className="btn btn--secondary form-editor__icon-btn"
                    aria-label={`Move section ${i + 1} down`}
                    disabled={i === def.sections.length - 1}
                    onClick={() => moveSection(i, 1)}
                  >
                    ↓
                  </button>
                  <button
                    type="button"
                    className="btn btn--danger form-editor__icon-btn"
                    aria-label={`Remove section ${i + 1}`}
                    disabled={def.sections.length === 1}
                    onClick={() => removeSection(i)}
                  >
                    ✕
                  </button>
                </div>
              )}
            </div>
            <SectionEditor section={s} onChange={(next) => updateSection(i, next)} />
          </li>
        ))}
      </ol>

      <AddSectionBar onAdd={addSection} />
    </div>
  );
}

// ── Identity panel ──────────────────────────────────────────────────────────────

function IdentityPanel({
  def,
  onChange,
  mode,
  identity,
  onIdentityChange,
  parentFormCode,
  onParentChange,
  knownParents,
  category,
  onCategoryChange,
}: Props) {
  const lockIdentity = mode === "edit"; // edit keeps the same identity, bumps version
  return (
    <section className="card form-editor__identity">
      <h3 className="form-editor__sub-heading">Identity</h3>
      <div className="form-editor__identity-grid">
        <label className="field">
          <span className="field__label">Form name *</span>
          <input
            className="field__input"
            value={def.form_name}
            maxLength={200}
            onChange={(e) => onChange({ ...def, form_name: e.target.value })}
          />
        </label>

        <label className="field">
          <span className="field__label">Form type (parent) *</span>
          <input
            className="field__input"
            value={parentFormCode}
            list="form-editor-parents"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            placeholder="e.g. jha"
            disabled={lockIdentity}
            onChange={(e) => onParentChange(slugifyKeyToSlug(e.target.value))}
          />
          <datalist id="form-editor-parents">
            {knownParents.map((p) => (
              <option key={p} value={p} />
            ))}
          </datalist>
        </label>

        <label className="field">
          <span className="field__label">
            Identity * <span className="muted">(lowercase, hyphens — e.g. jha-night)</span>
          </span>
          <input
            className="field__input"
            value={identity}
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            placeholder="e.g. jha-night"
            disabled={lockIdentity}
            onChange={(e) => onIdentityChange(slugifyKeyToSlug(e.target.value))}
          />
        </label>

        <label className="field">
          <span className="field__label">Variant label</span>
          <input
            className="field__input"
            value={def.variant_label ?? ""}
            placeholder="(none)"
            maxLength={200}
            onChange={(e) =>
              onChange({ ...def, variant_label: e.target.value.trim() === "" ? null : e.target.value })
            }
          />
        </label>

        <label className="field">
          <span className="field__label">Archetype *</span>
          <select
            className="field__input"
            value={def.archetype}
            onChange={(e) => onChange({ ...def, archetype: e.target.value })}
          >
            {ARCHETYPES.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
        </label>

        <label className="field">
          <span className="field__label">Workflow *</span>
          <select
            className="field__input"
            value={category}
            disabled={mode !== "create"}
            onChange={(e) => onCategoryChange(e.target.value)}
          >
            {WORKFLOWS_ORDERED.map((w) => (
              <option key={w.id} value={w.id}>
                {w.label}
              </option>
            ))}
          </select>
          {mode !== "create" && (
            <p className="muted form-editor__hint">
              A form type&rsquo;s workflow is changed from its card (&ldquo;Change workflow&rdquo;).
            </p>
          )}
        </label>

        <div className="field">
          <span className="field__label">Form code (derived)</span>
          <output className="form-editor__derived">{def.form_code || "—"}</output>
          <p className="muted form-editor__hint">
            {mode === "edit"
              ? `Version bumped to v${def.version}.`
              : "New identity — version 1."}
          </p>
        </div>
      </div>
    </section>
  );
}

/** Identity / parent inputs accept a hyphen slug, not snake_case — keep it permissive
 *  while typing (lowercase + collapse junk to a single hyphen, trim leading hyphens). */
function slugifyKeyToSlug(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9-]+/g, "-")
    .replace(/^-+/, "")
    .slice(0, 80);
}

// ── Add-section toolbar ───────────────────────────────────────────────────────

function AddSectionBar({ onAdd }: { onAdd: (t: SectionType) => void }) {
  return (
    <div className="form-editor__add-bar">
      <span className="field__label">Add a section</span>
      <div className="form-editor__add-buttons">
        {SECTION_TYPES.map((t) => (
          <button key={t} type="button" className="btn btn--secondary" onClick={() => onAdd(t)}>
            + {SECTION_TYPE_LABELS[t]}
          </button>
        ))}
      </div>
    </div>
  );
}

// ── Section editor (dispatch by type) ─────────────────────────────────────────

function SectionEditor({ section, onChange }: { section: Section; onChange: (s: Section) => void }) {
  switch (section.type) {
    case "header":
      return <FieldListEditor fields={section.fields} onChange={(fields) => onChange({ ...section, fields })} keyHint="field" />;
    case "static_text":
      return <StaticTextEditor section={section} onChange={onChange} />;
    case "repeating_table":
    case "signature_table":
      return <TableEditor section={section} onChange={onChange} />;
    case "checklist":
      return <ChecklistEditor section={section} onChange={onChange} />;
    case "freeform":
      return <FreeformEditor section={section} onChange={onChange} />;
    case "content_blocks":
      return <ContentBlocksEditor section={section} onChange={onChange} />;
    // guidance / form_link (SOP daily form, slice D1) are authored in the form
    // DEFINITION via the git publish pipeline — the builder shows them read-only so an
    // Edit of a form that carries them (daily-report-v2) never crashes the section list.
    //
    // `additional_photos` was MISSING here and in editorValidation.ts, while editorModel.ts
    // already listed it in BOTH of its registries — so that section rendered with no body at
    // all (the switch fell through to undefined) rather than saying why it cannot be edited.
    // tsc cannot catch it: this function's return type admits undefined and the project does
    // not set `noImplicitReturns`, so a missing case is silent. The readonly fixture test now
    // covers EVERY member of READ_ONLY_SECTION_TYPES instead of a sample.
    case "guidance":
    case "form_link":
    case "job_requirements":
    case "expected_materials":
    case "additional_photos":
      return (
        <p className="muted">
          This section is maintained in the form definition (publish pipeline) and is not
          editable in the builder. Other sections of this form can still be edited.
        </p>
      );
  }
}

// Common: a snake_case section "key" input (table/checklist/freeform/content_blocks).
function SectionKeyField({ value, onChange }: { value: string; onChange: (k: string) => void }) {
  return (
    <label className="field">
      <span className="field__label">Section key <span className="muted">(snake_case)</span></span>
      <input
        className="field__input"
        value={value}
        autoCapitalize="none"
        autoCorrect="off"
        spellCheck={false}
        onChange={(e) => onChange(slugifyKey(e.target.value))}
      />
    </label>
  );
}

function TitleField({ value, onChange }: { value: string | undefined; onChange: (t: string | undefined) => void }) {
  return (
    <label className="field">
      <span className="field__label">Title <span className="muted">(optional)</span></span>
      <input
        className="field__input"
        value={value ?? ""}
        onChange={(e) => onChange(e.target.value === "" ? undefined : e.target.value)}
      />
    </label>
  );
}

// ── Field-list editor (header fields + table columns) ─────────────────────────

function FieldListEditor({
  fields,
  onChange,
  keyHint,
}: {
  fields: Field[];
  onChange: (f: Field[]) => void;
  keyHint: string;
}) {
  const update = (i: number, next: Field) => onChange(fields.map((f, idx) => (idx === i ? next : f)));
  const add = () => onChange([...fields, blankField(`${keyHint}_${fields.length + 1}`)]);
  const remove = (i: number) => onChange(fields.filter((_, idx) => idx !== i));
  return (
    <div className="form-editor__fields">
      {fields.map((f, i) => (
        <FieldEditor
          key={i}
          field={f}
          onChange={(next) => update(i, next)}
          onRemove={fields.length > 1 ? () => remove(i) : undefined}
        />
      ))}
      <button type="button" className="btn btn--secondary" onClick={add}>
        + Add field
      </button>
    </div>
  );
}

function FieldEditor({
  field,
  onChange,
  onRemove,
}: {
  field: Field;
  onChange: (f: Field) => void;
  onRemove?: () => void;
}) {
  const setInput = (input: Input) => {
    const next: Field = { ...field, input };
    if (input === "select" && (!next.options || next.options.length === 0)) next.options = [""];
    if (input !== "select") delete next.options;
    onChange(next);
  };
  return (
    <div className="form-editor__field-row">
      <label className="field form-editor__field-cell">
        <span className="field__label">Label</span>
        <input className="field__input" value={field.label} onChange={(e) => onChange({ ...field, label: e.target.value })} />
      </label>
      <label className="field form-editor__field-cell">
        <span className="field__label">Key</span>
        <input
          className="field__input"
          value={field.key}
          autoCapitalize="none"
          spellCheck={false}
          onChange={(e) => onChange({ ...field, key: slugifyKey(e.target.value) })}
        />
      </label>
      <label className="field form-editor__field-cell">
        <span className="field__label">Input</span>
        <select className="field__input" value={field.input} onChange={(e) => setInput(e.target.value as Input)}>
          {FIELD_INPUTS.map((inp) => (
            <option key={inp} value={inp}>
              {inp}
            </option>
          ))}
        </select>
      </label>
      <label className="form-editor__field-required">
        <input
          type="checkbox"
          checked={field.required ?? false}
          onChange={(e) => onChange({ ...field, required: e.target.checked || undefined })}
        />
        <span>Required</span>
      </label>
      {field.input === "select" ? (
        <div className="form-editor__field-options">
          <OptionsEditor options={field.options ?? []} onChange={(options) => onChange({ ...field, options })} />
        </div>
      ) : null}
      {onRemove ? (
        <button type="button" className="btn btn--danger form-editor__icon-btn form-editor__field-remove" aria-label="Remove field" onClick={onRemove}>
          ✕
        </button>
      ) : null}
    </div>
  );
}

function OptionsEditor({
  options,
  onChange,
  label = "Options",
}: { options: string[]; onChange: (o: string[]) => void; label?: string }) {
  const update = (i: number, val: string) => onChange(options.map((o, idx) => (idx === i ? val : o)));
  return (
    <div className="form-editor__options">
      <span className="field__label">{label}</span>
      {options.map((o, i) => (
        <div key={i} className="form-editor__option-row">
          <input className="field__input" value={o} placeholder="option value" onChange={(e) => update(i, e.target.value)} />
          <button
            type="button"
            className="btn btn--danger form-editor__icon-btn"
            aria-label={`Remove option ${i + 1}`}
            disabled={options.length === 1}
            onClick={() => onChange(options.filter((_, idx) => idx !== i))}
          >
            ✕
          </button>
        </div>
      ))}
      <button type="button" className="btn btn--secondary" onClick={() => onChange([...options, ""])}>
        + Add option
      </button>
    </div>
  );
}

// ── Static-text editor ────────────────────────────────────────────────────────

function StaticTextEditor({
  section,
  onChange,
}: {
  section: Extract<Section, { type: "static_text" }>;
  onChange: (s: Section) => void;
}) {
  return (
    <div className="form-editor__section-body">
      <label className="field">
        <span className="field__label">Text</span>
        <textarea
          className="field__textarea"
          value={section.text}
          onChange={(e) => onChange({ ...section, text: e.target.value })}
        />
      </label>
      <label className="field">
        <span className="field__label">Emphasis</span>
        <select
          className="field__input"
          value={section.emphasis ?? "heading"}
          onChange={(e) => onChange({ ...section, emphasis: e.target.value as "footer" | "heading" | "legal" })}
        >
          <option value="heading">heading</option>
          <option value="footer">footer</option>
          <option value="legal">legal</option>
        </select>
      </label>
    </div>
  );
}

// ── Table editor (repeating_table + signature_table) ──────────────────────────

function TableEditor({
  section,
  onChange,
}: {
  section: Extract<Section, { type: "repeating_table" | "signature_table" }>;
  onChange: (s: Section) => void;
}) {
  const sigNote =
    section.type === "signature_table" ? (
      <p className="jha__notice">Signature tables need <strong>exactly one</strong> signature column.</p>
    ) : null;
  return (
    <div className="form-editor__section-body">
      <SectionKeyField value={section.key} onChange={(key) => onChange({ ...section, key })} />
      <TitleField value={section.title} onChange={(title) => onChange({ ...section, title })} />
      {sigNote}
      <span className="field__label">Columns</span>
      <FieldListEditor
        fields={section.columns}
        onChange={(columns) => onChange({ ...section, columns })}
        keyHint="col"
      />
      <div className="form-editor__inline-fields">
        <label className="field">
          <span className="field__label">Min rows</span>
          <input
            className="field__input"
            type="number"
            min={0}
            value={section.min_rows ?? 1}
            onChange={(e) => onChange({ ...section, min_rows: Math.max(0, Number(e.target.value) || 0) })}
          />
        </label>
        <label className="form-editor__field-required">
          <input
            type="checkbox"
            checked={section.allow_add !== false}
            onChange={(e) => onChange({ ...section, allow_add: e.target.checked })}
          />
          <span>Allow adding rows</span>
        </label>
      </div>
    </div>
  );
}

// ── Checklist editor ──────────────────────────────────────────────────────────

function ChecklistEditor({
  section,
  onChange,
}: {
  section: Extract<Section, { type: "checklist" }>;
  onChange: (s: Section) => void;
}) {
  const updateGroup = (i: number, g: Group) =>
    onChange({ ...section, groups: section.groups.map((x, idx) => (idx === i ? g : x)) });
  const addGroup = () =>
    onChange({ ...section, groups: [...section.groups, blankGroup(`group_${section.groups.length + 1}`)] });
  const removeGroup = (i: number) =>
    onChange({ ...section, groups: section.groups.filter((_, idx) => idx !== i) });
  return (
    <div className="form-editor__section-body">
      <SectionKeyField value={section.key} onChange={(key) => onChange({ ...section, key })} />
      <TitleField value={section.title} onChange={(title) => onChange({ ...section, title })} />
      <div className="form-editor__groups">
        {section.groups.map((g, i) => (
          <GroupEditor
            key={i}
            group={g}
            onChange={(next) => updateGroup(i, next)}
            onRemove={section.groups.length > 1 ? () => removeGroup(i) : undefined}
          />
        ))}
      </div>
      <button type="button" className="btn btn--secondary" onClick={addGroup}>
        + Add group
      </button>
    </div>
  );
}

export function GroupEditor({ group, onChange, onRemove }: { group: Group; onChange: (g: Group) => void; onRemove?: () => void }) {
  const updateItem = (i: number, it: Item) =>
    onChange({ ...group, items: group.items.map((x, idx) => (idx === i ? it : x)) });
  const addItem = () => onChange({ ...group, items: [...group.items, blankItem(`${group.key}_item_${group.items.length + 1}`)] });
  const removeItem = (i: number) => onChange({ ...group, items: group.items.filter((_, idx) => idx !== i) });
  return (
    <div className="form-editor__group">
      <div className="form-editor__inline-fields">
        <label className="field">
          <span className="field__label">Group label</span>
          <input className="field__input" value={group.label} onChange={(e) => onChange({ ...group, label: e.target.value })} />
        </label>
        <label className="field">
          <span className="field__label">Group key</span>
          <input
            className="field__input"
            value={group.key}
            autoCapitalize="none"
            spellCheck={false}
            onChange={(e) => onChange({ ...group, key: slugifyKey(e.target.value) })}
          />
        </label>
        {onRemove ? (
          <button type="button" className="btn btn--danger form-editor__icon-btn" aria-label="Remove group" onClick={onRemove}>
            ✕
          </button>
        ) : null}
      </div>
      <OptionsEditor label="Response scale" options={group.scale} onChange={(scale) => onChange({ ...group, scale })} />
      <label className="form-editor__field-required">
        <input
          type="checkbox"
          checked={group.comment_per_item ?? false}
          onChange={(e) => onChange({ ...group, comment_per_item: e.target.checked || undefined })}
        />
        <span>Comment box on each item</span>
      </label>
      <div className="form-editor__items">
        {group.items.map((it, i) => (
          <ItemEditor
            key={i}
            item={it}
            onChange={(next) => updateItem(i, next)}
            onRemove={group.items.length > 1 ? () => removeItem(i) : undefined}
          />
        ))}
        <button type="button" className="btn btn--secondary" onClick={addItem}>
          + Add item
        </button>
      </div>
    </div>
  );
}

function ItemEditor({ item, onChange, onRemove }: { item: Item; onChange: (it: Item) => void; onRemove?: () => void }) {
  const setKind = (kind: string) => {
    const next: Item = { ...item, kind: kind === "rated" ? undefined : (kind as Item["kind"]) };
    if (kind === "circle_one" && (!next.options || next.options.length === 0)) next.options = [""];
    if (kind !== "circle_one") delete next.options;
    onChange(next);
  };
  return (
    <div className="form-editor__item-row">
      <label className="field form-editor__field-cell">
        <span className="field__label">Item label</span>
        <input className="field__input" value={item.label} onChange={(e) => onChange({ ...item, label: e.target.value })} />
      </label>
      <label className="field form-editor__field-cell">
        <span className="field__label">Key</span>
        <input
          className="field__input"
          value={item.key}
          autoCapitalize="none"
          spellCheck={false}
          onChange={(e) => onChange({ ...item, key: slugifyKey(e.target.value) })}
        />
      </label>
      <label className="field form-editor__field-cell">
        <span className="field__label">Kind</span>
        <select className="field__input" value={item.kind ?? "rated"} onChange={(e) => setKind(e.target.value)}>
          {ITEM_KINDS.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
      </label>
      {item.kind === "circle_one" ? (
        <div className="form-editor__field-options">
          <OptionsEditor options={item.options ?? []} onChange={(options) => onChange({ ...item, options })} />
        </div>
      ) : null}
      {onRemove ? (
        <button type="button" className="btn btn--danger form-editor__icon-btn form-editor__field-remove" aria-label="Remove item" onClick={onRemove}>
          ✕
        </button>
      ) : null}
    </div>
  );
}

// ── Freeform editor ───────────────────────────────────────────────────────────

function FreeformEditor({
  section,
  onChange,
}: {
  section: Extract<Section, { type: "freeform" }>;
  onChange: (s: Section) => void;
}) {
  return (
    <div className="form-editor__section-body">
      <SectionKeyField value={section.key} onChange={(key) => onChange({ ...section, key })} />
      <label className="field">
        <span className="field__label">Label</span>
        <input className="field__input" value={section.label} onChange={(e) => onChange({ ...section, label: e.target.value })} />
      </label>
      <label className="field">
        <span className="field__label">Input</span>
        <select
          className="field__input"
          value={section.input ?? "textarea"}
          onChange={(e) => onChange({ ...section, input: e.target.value as "textarea" | "text" })}
        >
          <option value="textarea">textarea</option>
          <option value="text">text</option>
        </select>
      </label>
    </div>
  );
}

// ── Content-blocks editor ─────────────────────────────────────────────────────

function ContentBlocksEditor({
  section,
  onChange,
}: {
  section: Extract<Section, { type: "content_blocks" }>;
  onChange: (s: Section) => void;
}) {
  const update = (i: number, b: ContentBlock) =>
    onChange({ ...section, blocks: section.blocks.map((x, idx) => (idx === i ? b : x)) });
  const add = () => onChange({ ...section, blocks: [...section.blocks, blankBlock()] });
  const remove = (i: number) => onChange({ ...section, blocks: section.blocks.filter((_, idx) => idx !== i) });
  return (
    <div className="form-editor__section-body">
      <SectionKeyField value={section.key} onChange={(key) => onChange({ ...section, key })} />
      <TitleField value={section.title} onChange={(title) => onChange({ ...section, title })} />
      <div className="form-editor__blocks">
        {section.blocks.map((b, i) => (
          <div key={i} className="form-editor__block">
            <label className="field">
              <span className="field__label">Heading <span className="muted">(optional)</span></span>
              <input className="field__input" value={b.heading ?? ""} onChange={(e) => update(i, { ...b, heading: e.target.value === "" ? undefined : e.target.value })} />
            </label>
            <label className="field">
              <span className="field__label">Body</span>
              <textarea className="field__textarea form-editor__block-body" value={b.body} onChange={(e) => update(i, { ...b, body: e.target.value })} />
            </label>
            {section.blocks.length > 1 ? (
              <button type="button" className="btn btn--danger form-editor__icon-btn" aria-label={`Remove block ${i + 1}`} onClick={() => remove(i)}>
                ✕
              </button>
            ) : null}
          </div>
        ))}
      </div>
      <button type="button" className="btn btn--secondary" onClick={add}>
        + Add block
      </button>
    </div>
  );
}
