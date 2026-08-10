import { memo } from "react";
import type { Dispatch, SetStateAction } from "react";
import { AdditionalPhotosSection } from "../components/AdditionalPhotosSection";
import { PhotoField } from "../components/PhotoField";
import { SignaturePad } from "../components/SignaturePad";
import { statusPill, rowTitle } from "../components/ExpectedMaterialsSection";
import { DAILY_STATUS_FAMILIES, type DailyRequirementItem } from "../lib/fieldops_daily_form";
import type { ExpectedMaterialRow } from "../lib/fieldops_expected_materials";
import { dayPhaseFor } from "./dayPhase";
import type { AdditionalPhotoRef } from "../lib/fieldops_daily_photos";
import type { Field, FormDefinition, Group, PhotoValue, Section } from "./types";

// The fill state, keyed per section:
//   header field key          -> string (signature field -> SVG path string)
//   repeating/signature table -> Array<Record<colKey, string>>
//   checklist section key     -> Record<itemKey, { response?: string; comment?: string }>
//   freeform section key      -> string
export type FormValues = Record<string, unknown>;

type Row = Record<string, string>;
type ChecklistState = Record<string, { response?: string; comment?: string }>;

// Envelope-bound header keys — the fill page provides these top-level (job
// dropdown + work-date picker), so the renderer skips them to avoid duplicate UI.
const ENVELOPE_KEYS = new Set(["work_date", "job"]);

/** Build the initial fill state for a definition (empty header fields, min_rows table rows). */
export function initialValues(def: FormDefinition): FormValues {
  const v: FormValues = {};
  for (const s of def.sections) {
    if (s.type === "header") {
      for (const f of s.fields) v[f.key] = f.input === "photo" ? [] : "";
    } else if (s.type === "repeating_table" || s.type === "signature_table") {
      const n = Math.max(1, s.min_rows ?? 1);
      v[s.key] = Array.from({ length: n }, () => emptyRow(s.columns));
    } else if (s.type === "checklist") {
      v[s.key] = {};
    } else if (s.type === "freeform") {
      v[s.key] = "";
    }
  }
  return v;
}

const emptyRow = (cols: Field[]): Row => Object.fromEntries(cols.map((c) => [c.key, ""]));

/** Deep-link adapter for `form_link` sections (SOP daily form, slice D1). The renderer
 *  itself never navigates or fetches — the HOST (the Daily tab, slice D2) supplies this
 *  to wire the "Create <form> →" button to the existing openForm prefill flow and the
 *  filed-indicator to the family-match loop-closure query. With NO adapter the button
 *  renders disabled with a "available from the Daily tab" helper — so the plain
 *  Submit-a-Form fill path stays inert and send-free. */
export interface FormLinkAdapter {
  /** Open the create-form flow for the linked parent form type. */
  open: (parentFormCode: string) => void;
  /** Filed indicator (e.g. "Filed ✓ 2:14 PM") for the linked parent, or null. */
  filedLabel?: (parentFormCode: string) => string | null;
}

/** One filed answer in a `job_requirements` section's values array (slice D4). SELF-DESCRIBING
 *  on purpose: the submission carries the label + kind it answered, so the filed payload (and the
 *  PDF rendered from it) is stable regardless of later requirement edits. note items ride along
 *  with an empty response (they were shown, not answered); confirm = "Confirmed" | "";
 *  text = the typed answer; form_link = "" (the linked form files as its OWN submission);
 *  number/date (D5) = the typed value as a string; select (D5) = the chosen option string.
 *  Every kind's response is a STRING — the PDF's generic label→response rows need no new
 *  handling when kinds are added. */
export interface JobRequirementResponse {
  label: string;
  kind: string;
  response: string;
}

/** One row of the v7 expected-materials day snapshot. All display strings — see
 *  `seedExpectedMaterialsSnapshot` for why this carries no ids. */
export interface ExpectedMaterialSnapshotEntry {
  material: string;
  part_number: string;
  expected: string;
  status: string;
  received: string;
}

/** A fresh (all-empty) values array for a fetched requirement set — the HOST seeds
 *  values[<section key>] with this when the items load, so a submission filed with zero
 *  interaction still carries the requirements it displayed. */
export function seedRequirementResponses(items: DailyRequirementItem[]): JobRequirementResponse[] {
  return items.map((it) => ({ label: it.label, kind: it.kind, response: "" }));
}

/** Delivery state in the words the report uses on screen. The 0059 `receipt_status` rollup wins
 *  when a mark exists; otherwise the coarse `status`, which also carries the orthogonal sticky
 *  `incident` flag. */
function receiptStatusLabel(r: ExpectedMaterialRow): string {
  if (r.receipt_status === "delivered") return "Delivered";
  if (r.receipt_status === "partial") return "Partially delivered";
  if (r.receipt_status === "not_delivered") return "Not delivered";
  if (r.status === "incident") return "Problem reported";
  if (r.status === "received") return "Received";
  return "Expected";
}

/** The day's expected-materials SNAPSHOT (v7) — what the report SHOWED, frozen into the values the
 *  submission files.
 *
 *  Self-describing display strings, deliberately, exactly like `seedRequirementResponses`: the PDF
 *  renders these generically, so a filed document keeps reading correctly however the live D1 list
 *  is later edited, and a new field renders without a form-definition change. Nothing here is an id
 *  or a foreign key — this is a printed record, not a join.
 *
 *  Why snapshot at all: the manager signs a report that displayed a set of materials in a
 *  particular delivery state, and every later mark rewrites that state, so it cannot be
 *  reconstructed from live data afterwards. v5/v6 filed nothing here; their PDFs are unchanged. */
export function seedExpectedMaterialsSnapshot(
  rows: ExpectedMaterialRow[],
): ExpectedMaterialSnapshotEntry[] {
  return rows.map((r) => ({
    material: r.material_name || r.description || "",
    part_number: r.part_number ?? "",
    expected: r.qty == null ? "" : `${r.qty}${r.unit ? ` ${r.unit}` : ""}`,
    status: receiptStatusLabel(r),
    received: r.qty_received_total == null ? "" : String(r.qty_received_total),
  }));
}

/** Adapter for `expected_materials` sections (Material receipts M2). Like FormLinkAdapter, the
 *  renderer itself never fetches or mutates — the HOST (the Daily tab) supplies the job's
 *  expected-material rows (M1's read) plus the two receipt actions, and owns the per-row busy
 *  state and any action error. With NO adapter the section renders NOTHING — the generic fill
 *  page (and every non-daily form) is unaffected. The section files NO form values of its own:
 *  the host's onConfirmReceipt appends a deliveries_received row; problems file as the
 *  material-incident form's OWN submission (deep-linked by the host). The live
 *  "Filed ✓" indicator for that incident form rides the EXISTING FormLinkAdapter.filedLabel
 *  ('material-incident' is a DAILY_STATUS_FAMILIES member since M2). */
export interface ExpectedMaterialsAdapter {
  /** The job's expected materials, seq order (fetch state — including errors — is the host's). */
  rows: ExpectedMaterialRow[];
  /** Rows with an in-flight receive/flag call — their action buttons render disabled. */
  busyIds: ReadonlySet<number>;
  /** A failed action's message, rendered inline in the section (never silent). */
  actionError?: string | null;
  /** "Confirm receipt" — the host calls the M1 receive route + appends the deliveries row. */
  onConfirmReceipt: (row: ExpectedMaterialRow) => void;
  /** "Report a problem →" — the host flags the row + deep-links material-incident prefilled. */
  onReportProblem: (row: ExpectedMaterialRow) => void;
  /** PR2 — "Materials tracking →", the deep link into the per-job Materials page (the full list
   *  with ship/delivery dates, scheduled loads and the delivery history). OPTIONAL, so the
   *  section renders exactly as before for any host with nowhere to navigate to — which is why
   *  this needs NO form-definition change: the mount's whole body is authored here, and the
   *  definition contributes only type/key/title. */
  onOpenMaterials?: () => void;
}

/** Adapter for `additional_photos` sections (DR-photo-pool Slice 1). The HOST (the Daily tab)
 *  supplies only the SCOPE — the placed job + report date the pool uploads bind to; everything
 *  else (upload / status chips / removal) is self-contained in AdditionalPhotosSection, which
 *  reads its refs from values[<section key>] and writes them back through setValues (so the
 *  host's draft machinery persists the tiny [{pool_id, caption?}] references for free — never
 *  photo bytes). With NO adapter the section renders NOTHING — the generic fill page (and every
 *  non-daily form) is unaffected. */
export interface AdditionalPhotosAdapter {
  jobId: string;
  workDate: string;
  /** The filed submission being AMENDED (the Daily tab's loadAmend), else null/absent. Threaded
   *  to the pool list read (`amends=`) so the amended report's own claimed rows chip
   *  "Photo on file ✓" instead of lying "missing" — the Worker verifies the uuid (same
   *  actor/job/date) before honoring it, so a stale/foreign value just degrades gracefully. */
  amendsUuid?: string | null;
}

interface Props {
  def: FormDefinition;
  values: FormValues;
  setValues: Dispatch<SetStateAction<FormValues>>;
  /** Optional D2 hook — see FormLinkAdapter. Absent on the generic fill page. */
  formLinks?: FormLinkAdapter;
  /** Optional D4 hook — the job's fetched per-job requirement items, rendered by any
   *  `job_requirements` section. Absent (the generic fill page) or empty → the section
   *  renders NOTHING, so every other form is unaffected. */
  requirements?: DailyRequirementItem[];
  /** Optional M2 hook — see ExpectedMaterialsAdapter. Absent on the generic fill page
   *  (the `expected_materials` section renders NOTHING without it). */
  expectedMaterials?: ExpectedMaterialsAdapter;
  /** Optional DR-photo-pool hook — see AdditionalPhotosAdapter. Absent on the generic fill
   *  page (the `additional_photos` section renders NOTHING without it). */
  additionalPhotos?: AdditionalPhotosAdapter;
  /** Optional, PRESENTATIONAL ONLY — the daily SOP's chronological day-rail (design
   *  refinement, 2026-07). When set (the Daily tab), guidance sections render with a
   *  slim left rail, and the five phase-opening sections (dayPhase.ts, derived from
   *  the definition's own headings) carry a time-of-day eyebrow. Absent (the generic
   *  fill page and every other form) → markup is byte-identical to before. */
  dayRail?: boolean;
  /** Fired once the first time a signature stroke completes in ANY pad in this form.
   *  Hosts OR it into their unsaved-work flag: with commit-on-Done a signature does not
   *  reach `setValues` until Done, so a user whose first action is signing was otherwise
   *  invisible to the beforeunload / popstate guards. */
  onDraftDirty?: () => void;
}

export function FormRenderer({ def, values, setValues, formLinks, requirements, expectedMaterials, additionalPhotos, dayRail, onDraftDirty }: Props) {
  const setField = (key: string, val: string) =>
    setValues((v) => ({ ...v, [key]: val }));

  // Photo header fields hold PhotoValue[] (not string) — see types.PhotoValue.
  const setPhotos = (key: string, next: PhotoValue[]) =>
    setValues((v) => ({ ...v, [key]: next }));

  // additional_photos sections hold AdditionalPhotoRef[] (pool references, never bytes).
  const setPhotoRefs = (key: string, next: AdditionalPhotoRef[]) =>
    setValues((v) => ({ ...v, [key]: next }));

  const setCell = (secKey: string, idx: number, colKey: string, val: string) =>
    setValues((v) => {
      const rows = [...((v[secKey] as Row[]) ?? [])];
      rows[idx] = { ...rows[idx], [colKey]: val };
      return { ...v, [secKey]: rows };
    });

  const addRow = (secKey: string, cols: Field[]) =>
    setValues((v) => ({ ...v, [secKey]: [...((v[secKey] as Row[]) ?? []), emptyRow(cols)] }));

  const removeRow = (secKey: string, idx: number) =>
    setValues((v) => {
      const rows = (v[secKey] as Row[]) ?? [];
      return rows.length > 1 ? { ...v, [secKey]: rows.filter((_, i) => i !== idx) } : v;
    });

  const setChecklist = (secKey: string, itemKey: string, patch: { response?: string; comment?: string }) =>
    setValues((v) => {
      const cl = { ...((v[secKey] as ChecklistState) ?? {}) };
      cl[itemKey] = { ...cl[itemKey], ...patch };
      return { ...v, [secKey]: cl };
    });

  // D4 — one requirement answered: rebuild the FULL self-describing array from the CURRENT item
  // set (preserving other answers by label+kind), so the written value always mirrors what the
  // manager saw — even if a draft predates a mid-day requirement edit.
  const setRequirement = (secKey: string, items: DailyRequirementItem[], targetId: number, response: string) =>
    setValues((v) => {
      const prev = Array.isArray(v[secKey]) ? (v[secKey] as JobRequirementResponse[]) : [];
      const next = items.map((it) => {
        if (it.id === targetId) return { label: it.label, kind: it.kind, response };
        const existing = prev.find((r) => r.label === it.label && r.kind === it.kind);
        return { label: it.label, kind: it.kind, response: existing?.response ?? "" };
      });
      return { ...v, [secKey]: next };
    });

  return (
    <div className="fr">
      {def.sections.map((s, i) => (
        <SectionView
          key={i}
          section={s}
          values={values}
          setField={setField}
          setPhotos={setPhotos}
          setCell={setCell}
          addRow={addRow}
          removeRow={removeRow}
          setChecklist={setChecklist}
          formLinks={formLinks}
          requirements={requirements}
          setRequirement={setRequirement}
          expectedMaterials={expectedMaterials}
          additionalPhotos={additionalPhotos}
          setPhotoRefs={setPhotoRefs}
          dayRail={dayRail}
          onDraftDirty={onDraftDirty}
        />
      ))}
    </div>
  );
}

interface SectionProps {
  section: Section;
  values: FormValues;
  setField: (k: string, v: string) => void;
  setPhotos: (k: string, next: PhotoValue[]) => void;
  setCell: (sec: string, idx: number, col: string, v: string) => void;
  addRow: (sec: string, cols: Field[]) => void;
  removeRow: (sec: string, idx: number) => void;
  setChecklist: (sec: string, item: string, patch: { response?: string; comment?: string }) => void;
  formLinks?: FormLinkAdapter;
  requirements?: DailyRequirementItem[];
  setRequirement: (sec: string, items: DailyRequirementItem[], targetId: number, response: string) => void;
  expectedMaterials?: ExpectedMaterialsAdapter;
  additionalPhotos?: AdditionalPhotosAdapter;
  setPhotoRefs: (sec: string, next: AdditionalPhotoRef[]) => void;
  dayRail?: boolean;
  onDraftDirty?: () => void;
}

function SectionView(p: SectionProps) {
  const s = p.section;
  switch (s.type) {
    case "header": {
      const fields = s.fields.filter((f) => !ENVELOPE_KEYS.has(f.key));
      if (fields.length === 0) return null; // whole header was envelope-bound
      return (
        <section className="fr__section">
          {s.title ? <h2 className="fr__section-title">{s.title}</h2> : null}
          <div className="fr__grid">
            {fields.map((f) =>
              f.input === "photo" ? (
                <PhotoField key={f.key} field={f}
                  photos={(p.values[f.key] as PhotoValue[]) ?? []}
                  onChange={(next) => p.setPhotos(f.key, next)} />
              ) : (
                <FieldView key={f.key} field={f} value={String(p.values[f.key] ?? "")}
                  onChange={(v) => p.setField(f.key, v)} onDraftDirty={p.onDraftDirty} />
              ))}
          </div>
        </section>
      );
    }
    // Value-independent sections short-circuit through the memo'd StaticSectionView (below) —
    // they read only the section object + the dayRail flag, never `values`, so a keystroke
    // re-render never re-renders them.
    case "static_text":
    case "content_blocks":
    case "guidance":
      return <StaticSectionView section={s} dayRail={p.dayRail} />;
    case "freeform":
      return (
        <section className="fr__section">
          <label className="field">
            <span className="field__label">{s.label}</span>
            <textarea className="field__textarea" value={String(p.values[s.key] ?? "")}
              onChange={(e) => p.setField(s.key, e.target.value)} />
          </label>
        </section>
      );
    case "repeating_table":
    case "signature_table":
      return <TableView section={s} rows={(p.values[s.key] as Row[]) ?? []}
        onCell={(i, c, v) => p.setCell(s.key, i, c, v)}
        onAdd={() => p.addRow(s.key, s.columns)} onRemove={(i) => p.removeRow(s.key, i)}
        onDraftDirty={p.onDraftDirty} />;
    case "checklist":
      return <ChecklistView section={s} state={(p.values[s.key] as ChecklistState) ?? {}}
        onChange={(item, patch) => p.setChecklist(s.key, item, patch)} />;
    // Deep link to another form type (slice D1). With no adapter (the generic fill
    // page) the button is disabled and explains where the live link lives; the Daily
    // tab (D2) supplies FormLinkAdapter to wire the real deep-link + filed indicator.
    // Per-job daily-form requirements (slice D4): the D1 overlay the HOST fetched (the
    // `requirements` prop). No prop / zero items → NOTHING renders (other forms unaffected).
    case "job_requirements": {
      const items = p.requirements ?? [];
      if (items.length === 0) return null;
      const current = Array.isArray(p.values[s.key]) ? (p.values[s.key] as JobRequirementResponse[]) : [];
      const responseFor = (it: DailyRequirementItem): string =>
        current.find((r) => r.label === it.label && r.kind === it.kind)?.response ?? "";
      return (
        <section className="fr__section fr__job-reqs">
          <h2 className="fr__section-title">{s.title ?? "Job-specific requirements"}</h2>
          {items.map((it) => {
            if (it.kind === "note") {
              // Guidance-paragraph style — read-only client instruction (no answer control).
              return <p key={it.id} className="fr__guidance-p">{it.label}</p>;
            }
            if (it.kind === "confirm") {
              const on = responseFor(it) === "Confirmed";
              return (
                <label key={it.id} className="field fr__req-confirm">
                  <input
                    type="checkbox"
                    checked={on}
                    onChange={(e) =>
                      p.setRequirement(s.key, items, it.id, e.target.checked ? "Confirmed" : "")}
                  />{" "}
                  <span className="field__label">{it.label}</span>
                </label>
              );
            }
            // text / number / date all capture a plain string answer through the same
            // self-describing {label, kind, response} shape (D5 added number + date) — only the
            // input control differs, so the filed values array (and the PDF's generic
            // label→response rows) need no new handling.
            if (it.kind === "text" || it.kind === "number" || it.kind === "date") {
              return (
                <label key={it.id} className="field">
                  <span className="field__label">{it.label}</span>
                  <input
                    className="field__input"
                    type={it.kind === "text" ? "text" : it.kind}
                    inputMode={it.kind === "number" ? "numeric" : undefined}
                    value={responseFor(it)}
                    onChange={(e) => p.setRequirement(s.key, items, it.id, e.target.value)}
                  />
                </label>
              );
            }
            // select (D5) — pick-one from the item's admin-authored options; the chosen option
            // string IS the response (the empty default files as "", i.e. unanswered).
            if (it.kind === "select") {
              const opts = it.options ?? [];
              return (
                <label key={it.id} className="field">
                  <span className="field__label">{it.label}</span>
                  <select
                    className="field__input"
                    value={responseFor(it)}
                    onChange={(e) => p.setRequirement(s.key, items, it.id, e.target.value)}
                  >
                    <option value="">— select —</option>
                    {opts.map((o, oi) => (
                      <option key={oi} value={o}>{o}</option>
                    ))}
                  </select>
                </label>
              );
            }
            // form_link — the existing deep-link affordance. The filed indicator only exists for
            // the DAILY_STATUS_FAMILIES the status endpoint reports; other catalog parents get
            // the link with an honest "no live indicator" note instead of a lying blank.
            const code = it.form_code;
            const tracked = code !== null && DAILY_STATUS_FAMILIES.includes(code);
            const filed = code !== null && tracked ? p.formLinks?.filedLabel?.(code) ?? null : null;
            return (
              <div key={it.id} className="fr__form-link">
                <button
                  type="button"
                  className="btn btn--primary"
                  disabled={!p.formLinks || code === null}
                  onClick={p.formLinks && code !== null ? () => p.formLinks?.open(code) : undefined}
                >
                  {it.label} →
                </button>
                {filed ? <span className="fr__form-link-filed">{filed}</span> : null}
                {!p.formLinks ? (
                  <p className="fr__form-link-helper muted">available from the Daily tab</p>
                ) : !tracked ? (
                  <p className="fr__form-link-helper muted">
                    No live filed indicator for this form type — check Form Request for filed copies.
                  </p>
                ) : null}
              </div>
            );
          })}
        </section>
      );
    }
    // Expected-materials receipt list (Material receipts M2): the M1 rows the HOST fetched
    // (the `expectedMaterials` adapter). No adapter → NOTHING renders — the generic fill page
    // and every other form are unaffected. The section files NO form values of its own:
    // "Confirm receipt" flips the D1 row + appends a deliveries_received table row (both the
    // host's duty); "Report a problem →" flags the row + deep-links the material-incident
    // form, which files as its OWN submission.
    case "expected_materials": {
      const em = p.expectedMaterials;
      if (!em) return null;
      // Live "Filed ✓" for the incident form this section deep-links to — material-incident
      // is a DAILY_STATUS_FAMILIES member (M2), served by the same status read form_link uses.
      const incidentFiled = p.formLinks?.filedLabel?.("material-incident") ?? null;
      return (
        <section className="fr__section fr__expected-materials">
          <h2 className="fr__section-title">{s.title ?? "Expected materials"}</h2>
          {em.onOpenMaterials ? (
            <p>
              <button type="button" className="btn btn--secondary" onClick={em.onOpenMaterials}>
                Materials tracking →
              </button>
            </p>
          ) : null}
          {incidentFiled ? (
            <p className="fr__form-link-filed">Material incident report: {incidentFiled}</p>
          ) : null}
          {em.actionError ? (
            <p className="banner banner--err" role="alert">
              {em.actionError}
            </p>
          ) : null}
          {em.rows.length === 0 ? (
            <p className="muted">No expected materials for this job.</p>
          ) : (
            <ul className="dash-tasklist">
              {em.rows.map((r) => {
                const pill = statusPill(r.status);
                const busy = em.busyIds.has(r.id);
                return (
                  <li key={r.id}>
                    <span className={pill.className}>{pill.label}</span> <strong>{rowTitle(r)}</strong>
                    {r.qty != null ? (
                      <span className="dash-chip">
                        {r.qty}
                        {r.unit ? ` ${r.unit}` : ""}
                      </span>
                    ) : r.unit ? (
                      <span className="dash-chip">{r.unit}</span>
                    ) : null}
                    {r.expected_date ? <span className="dash-chip">expected {r.expected_date}</span> : null}
                    {r.status === "expected" ? (
                      <div className="dash-row">
                        <button
                          type="button"
                          className="btn btn--primary"
                          disabled={busy}
                          aria-label={`Confirm receipt of ${rowTitle(r)}`}
                          onClick={() => em.onConfirmReceipt(r)}
                        >
                          {busy ? "Working…" : "Confirm receipt"}
                        </button>{" "}
                        <button
                          type="button"
                          className="btn btn--secondary"
                          disabled={busy}
                          aria-label={`Report a problem with ${rowTitle(r)}`}
                          onClick={() => em.onReportProblem(r)}
                        >
                          Report a problem →
                        </button>
                      </div>
                    ) : (
                      // Received/incident rows are receipt RECORDS: pill + who/when (+ note).
                      <div className="dash-card__sub muted">
                        {r.status === "received" ? "Received" : "Flagged"}
                        {r.received_at ? ` ${new Date(r.received_at * 1000).toLocaleString()}` : ""}
                        {r.received_by_name ? ` by ${r.received_by_name}` : ""}
                        {r.qty_received != null ? ` · qty received ${r.qty_received}` : ""}
                        {r.note ? ` · ${r.note}` : ""}
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      );
    }
    // Additional-photos pool mount (DR-photo-pool Slice 1): renders ONLY when the HOST supplies
    // the AdditionalPhotosAdapter (the Daily tab's job + date scope) — the generic fill page and
    // every non-daily form are unaffected. The section's value is the tiny pool-reference list
    // (values[<key>] = [{pool_id, caption?}]); the bytes went to the pool via their own bounded
    // uploads (the inline site_photos field above it is payload-budgeted and stays untouched).
    case "additional_photos": {
      const ap = p.additionalPhotos;
      if (!ap) return null;
      const refs = Array.isArray(p.values[s.key]) ? (p.values[s.key] as AdditionalPhotoRef[]) : [];
      return (
        <AdditionalPhotosSection
          title={s.title}
          jobId={ap.jobId}
          workDate={ap.workDate}
          amendsUuid={ap.amendsUuid ?? null}
          refs={refs}
          onChange={(next) => p.setPhotoRefs(s.key, next)}
        />
      );
    }
    case "form_link": {
      const filed = p.formLinks?.filedLabel?.(s.parent_form_code) ?? null;
      return (
        <section className="fr__section fr__form-link">
          <button
            type="button"
            className="btn btn--primary"
            disabled={!p.formLinks}
            onClick={p.formLinks ? () => p.formLinks?.open(s.parent_form_code) : undefined}
          >
            {/* The arrow is button CHROME (the definition label stays plain text so the
                PDF renderer / needle tests never depend on a non-WinAnsi glyph). */}
            {s.label} →
          </button>
          {filed ? <span className="fr__form-link-filed">{filed}</span> : null}
          {p.formLinks ? (
            s.helper ? <p className="fr__form-link-helper muted">{s.helper}</p> : null
          ) : (
            <p className="fr__form-link-helper muted">available from the Daily tab</p>
          )}
        </section>
      );
    }
  }
}

// ── Value-independent sections (optimization #5) ─────────────────────────────────────────────────
// static_text / content_blocks / guidance read ONLY the section object (a referentially-stable
// slice of the build-time-bundled definition) plus the stable dayRail flag — never `values` and
// no callbacks. React.memo's default shallow compare therefore short-circuits all of them on the
// per-keystroke FormRenderer re-render: the SOP daily form alone carries ~20 guidance sections
// that would otherwise re-render on every key press. Markup is byte-identical to the pre-memo
// inline cases (the day-rail / eyebrow contract included). Interactive sections are deliberately
// NOT memoized — values-slicing comparators + useCallback plumbing without profile evidence
// (rejected in the optimization plan, §14 preservation-over-refactor).
const StaticSectionView = memo(function StaticSectionView({
  section: s,
  dayRail,
}: {
  section: Extract<Section, { type: "static_text" | "content_blocks" | "guidance" }>;
  dayRail?: boolean;
}) {
  switch (s.type) {
    case "static_text":
      return <p className={`fr__static fr__static--${s.emphasis ?? "heading"}`}>{s.text}</p>;
    case "content_blocks":
      return (
        <section className="fr__section fr__content">
          {s.title ? <h2 className="fr__section-title">{s.title}</h2> : null}
          {s.blocks.map((b, i) => (
            <div className="fr__content-block" key={i}>
              {b.heading ? <h3 className="fr__content-heading">{b.heading}</h3> : null}
              <p className="fr__content-body">{b.body}</p>
            </div>
          ))}
        </section>
      );
    // Read-only SOP guidance (slice D1): heading + paragraphs / bullet lists / styled
    // callouts, VERBATIM from the definition. Contributes no fill state.
    // With the host's `dayRail` (the Daily tab): a presentational left rail on every
    // guidance section + a time-of-day eyebrow on the five phase openers (dayPhase.ts).
    // The eyebrow is aria-hidden — it restates the heading's own phase for the eye only.
    case "guidance": {
      const phase = dayRail ? dayPhaseFor(s.heading) : null;
      return (
        <section className={`fr__section fr__guidance${dayRail ? " fr__guidance--rail" : ""}`}>
          {phase ? (
            <p className="fr__day-eyebrow" aria-hidden="true">
              {phase}
            </p>
          ) : null}
          <h2 className="fr__section-title">{s.heading}</h2>
          {s.blocks.map((b, i) => {
            if (b.type === "p") return <p key={i} className="fr__guidance-p">{b.text}</p>;
            if (b.type === "bullets") {
              return (
                <ul key={i} className="fr__guidance-bullets">
                  {b.items.map((item, j) => <li key={j}>{item}</li>)}
                </ul>
              );
            }
            // callout — visually distinct per style (gold legal look for note/quality,
            // danger edge for critical); the TEXT itself already carries its own
            // "CRITICAL RULE:" / "QUALITY RULE:" / "NOTE:" prefix verbatim.
            return (
              <div key={i} role="note" className={`fr__callout fr__callout--${b.style}`}>
                {b.text}
              </div>
            );
          })}
        </section>
      );
    }
  }
});

function FieldView({ field, value, onChange, onDraftDirty }: { field: Field; value: string; onChange: (v: string) => void; onDraftDirty?: () => void }) {
  if (field.input === "signature") {
    return (
      <div className="field">
        <span className="field__label">{field.label}</span>
        {/* CONTROLLED: `value` already arrives String()-normalized. Without it an amend
            repopulates form values while the pad renders blank — and the old signature
            gets filed under a preview that says "Tap to sign". */}
        <SignaturePad value={value} onChange={(svg, empty) => onChange(empty ? "" : svg)}
          onDraftDirty={onDraftDirty} />
      </div>
    );
  }
  if (field.input === "select") {
    return (
      <label className="field">
        <span className="field__label">{field.label}{field.required ? " *" : ""}</span>
        <select className="field__input" value={value} onChange={(e) => onChange(e.target.value)}>
          <option value="">—</option>
          {(field.options ?? []).map((o) => <option key={o} value={o}>{o}</option>)}
        </select>
      </label>
    );
  }
  if (field.input === "textarea") {
    return (
      <label className="field">
        <span className="field__label">{field.label}{field.required ? " *" : ""}</span>
        <textarea className="field__textarea" value={value} onChange={(e) => onChange(e.target.value)} />
      </label>
    );
  }
  // text / date / time / number
  return (
    <label className="field">
      <span className="field__label">{field.label}{field.required ? " *" : ""}</span>
      <input className="field__input" type={field.input} value={value}
        onChange={(e) => onChange(e.target.value)} />
    </label>
  );
}

function TableView({ section, rows, onCell, onAdd, onRemove, onDraftDirty }: {
  section: Extract<Section, { type: "repeating_table" | "signature_table" }>;
  rows: Row[]; onCell: (i: number, c: string, v: string) => void;
  onAdd: () => void; onRemove: (i: number) => void; onDraftDirty?: () => void;
}) {
  return (
    <section className="fr__section">
      {section.title ? <h2 className="fr__section-title">{section.title}</h2> : null}
      <div className="fr__rows">
        {rows.map((row, i) => (
          <div className="fr__row" key={i}>
            {rows.length > 1 ? (
              <button type="button" className="fr__row-remove" aria-label={`Remove row ${i + 1}`}
                onClick={() => onRemove(i)}>✕</button>
            ) : null}
            {section.columns.map((c) => (
              <div className="fr__cell" key={c.key}>
                <span className="fr__cell-label">{c.label}</span>
                {c.input === "signature" ? (
                  /* CONTROLLED — mirrors the sibling <input value=...> below. Rows are
                     index-keyed, so removing a row shifts values up while React reuses
                     the instance: an uncontrolled pad would paint the DELETED person's
                     signature onto the next row. */
                  <SignaturePad value={String(row[c.key] ?? "")}
                    onChange={(svg, empty) => onCell(i, c.key, empty ? "" : svg)}
                    onDraftDirty={onDraftDirty} />
                ) : (
                  <input className="field__input"
                    type={c.input === "date" || c.input === "time" || c.input === "number" ? c.input : "text"}
                    value={row[c.key] ?? ""} onChange={(e) => onCell(i, c.key, e.target.value)} />
                )}
              </div>
            ))}
          </div>
        ))}
      </div>
      {section.allow_add !== false ? (
        <button type="button" className="btn btn--secondary" onClick={onAdd}>+ Add row</button>
      ) : null}
    </section>
  );
}

function ChecklistView({ section, state, onChange }: {
  section: Extract<Section, { type: "checklist" }>;
  state: ChecklistState; onChange: (item: string, patch: { response?: string; comment?: string }) => void;
}) {
  return (
    <section className="fr__section">
      {section.title ? <h2 className="fr__section-title">{section.title}</h2> : null}
      {section.groups.map((g) => <GroupView key={g.key} group={g} state={state} onChange={onChange} />)}
    </section>
  );
}

function GroupView({ group, state, onChange }: {
  group: Group; state: ChecklistState; onChange: (item: string, patch: { response?: string; comment?: string }) => void;
}) {
  return (
    <div className="fr__group">
      <h3 className="fr__group-title">{group.label}</h3>
      {group.items.map((it) => {
        const cur = state[it.key] ?? {};
        const showComment = it.comment ?? group.comment_per_item ?? false;
        return (
          <div className="fr__item" key={it.key}>
            <span className="fr__item-label">{it.label}</span>
            <div className="fr__item-control">
              {it.kind === "numeric" ? (
                <input className="field__input fr__item-num" type="number" value={cur.response ?? ""}
                  onChange={(e) => onChange(it.key, { response: e.target.value })} />
              ) : it.kind === "text" ? (
                <input className="field__input" type="text" value={cur.response ?? ""}
                  onChange={(e) => onChange(it.key, { response: e.target.value })} />
              ) : (
                <div className="fr__scale" role="radiogroup" aria-label={it.label}>
                  {/* Scale buttons are a TRUE TOGGLE (operator directive 2026-07-03 — the daily
                      field report's "Confirmed" buttons could be set but never un-set): clicking
                      the selected option clears the response back to "" (unanswered), the only
                      un-confirm path a one-option scale has. The filed value shape is unchanged:
                      response stays a string, and "" is the established unanswered value
                      downstream (the initial state, the D4 confirm kind, and the PDF renderer's
                      blank-vs-N/A cell). Styling + aria-pressed derive from cur.response, so the
                      confirmed ↔ neutral visual states revert for free. */}
                  {(it.scale ?? (it.kind === "circle_one" ? it.options : group.scale) ?? []).map((opt) => (
                    <button type="button" key={opt}
                      className={`fr__scale-opt${cur.response === opt ? " fr__scale-opt--on" : ""}`}
                      aria-pressed={cur.response === opt}
                      onClick={() => onChange(it.key, { response: cur.response === opt ? "" : opt })}>{opt}</button>
                  ))}
                </div>
              )}
            </div>
            {showComment ? (
              <input className="field__input fr__item-comment" type="text" placeholder="Comments"
                value={cur.comment ?? ""} onChange={(e) => onChange(it.key, { comment: e.target.value })} />
            ) : null}
          </div>
        );
      })}
    </div>
  );
}
