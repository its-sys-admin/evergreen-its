import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { useAuth } from "../lib/auth";
import * as checklist from "../lib/fieldops_checklist";
import type { PersonnelRow } from "../lib/fieldops_personnel";
import { fetchJobList, type JobRow } from "../lib/fieldops_jobtracker";
import { statusLabel } from "../lib/labels";
import { PageShell } from "../components/PageShell";
import { ChecklistItemRow } from "../components/ChecklistItemRow";
import { ChecklistItemForm,
  EMPTY_ITEM,
  isFormBearing,
  itemInputFromRow,
  itemMetaLabel,
  nextSeq,
  parseRequiresPhoto,
  planRenumber, ConfirmDelete } from "../components/ChecklistItemForm";

// R8 — the admin "Checklists" page (view key 'fieldops-inspections' unchanged), REDESIGNED to the
// Forms form-builder pattern: a master-detail library (a selectable checklist list beside a rich
// detail pane) whose detail is a form-editor SPLIT — the item editor on the left, an ALWAYS-ON
// "Preview as assignee" on the right (the assignee renders through the REAL ChecklistItemRow, so
// what you build is what they see, live, as you edit). The lifecycle actions (rename / deactivate /
// delete) moved off the list rows into the selected checklist's detail header, so the list stays a
// clean navigator — exactly the Forms catalog → detail shape. Below the authoring surface sits the
// "Assign & track" band (assign a checklist + the outstanding-assignments monitor), the checklist
// analog of the form builder's publish + publish-monitor.
//
// INSPECTIONS-ONLY since D2: the generic_inspection library only. The company-wide "Default daily
// checklist" editor is RETIRED — the daily content lives in the daily-report-v2 FORM DEFINITION
// (edited via the form builder / publish pipeline). Gated cap.checklist.manage (admin); every call
// is re-gated server-side (Invariant 2) — caps here drive UI affordances only. Send-free (D1
// reads/writes). Feedback is per-section inline; loading is rendered distinct from empty everywhere.
// The shared item components (ChecklistItemForm / ChecklistItemRow / ConfirmDelete) are reused
// verbatim — this slice is a page-layout redesign, not a data or component-contract change.

type Msg = { ok: boolean; text: string };

function MsgLine({ msg }: { msg: Msg | null }) {
  if (!msg) return null;
  return <p className={`banner ${msg.ok ? "banner--ok" : "banner--err"}`}>{msg.text}</p>;
}

const NOOP = () => {};

// Synthesize the assignee-side item-state shape from an authoring row so the read-only preview can
// render through the REAL assignee component (ChecklistItemRow) without touching it: open status,
// no completion data, disabled controls via busy=true.
function previewState(it: checklist.DefaultItem): checklist.ChecklistItemState {
  return {
    id: it.id,
    source_item_id: it.id,
    item_type: it.item_type,
    label: it.label,
    form_code: it.form_code,
    target_count: it.target_count,
    status: "open",
    note: null,
    photo_ref: null,
    completed_by: null,
    completed_at: null,
    value_num: null,
    filed_by: null,
    photo_status: null,
    requires_photo: parseRequiresPhoto(it.config_json),
  };
}

// ── Detail split — one template's item editor (build pane) + the always-on assignee preview ───────
// The form-builder split: the item list + add/edit form on the left, and the live assignee preview
// on the right (sticky on wide screens). No preview TOGGLE — both are visible at once, so an edit is
// reflected in the preview the moment its reload lands. Item add/edit/reorder/remove behavior and
// every aria-label are unchanged from the pre-redesign editor.
function TemplateItemsEditor({
  template,
  onTemplatesChanged,
}: {
  template: checklist.InspectionTemplate;
  onTemplatesChanged: () => void;
}) {
  const [detail, setDetail] = useState<checklist.InspectionDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<Msg | null>(null);
  const [addDraft, setAddDraft] = useState<checklist.ItemInput>(EMPTY_ITEM);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editDraft, setEditDraft] = useState<checklist.ItemInput>(EMPTY_ITEM);

  const templateId = template.id;

  async function reload() {
    try {
      setDetail(await checklist.fetchInspectionTemplate(templateId));
    } catch {
      setMsg({ ok: false, text: "Could not load the checklist." });
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    setLoading(true);
    setDetail(null);
    setEditingId(null);
    void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [templateId]);

  async function run(fn: () => Promise<unknown>, okText: string, alsoTemplates = false) {
    if (busy) return;
    setBusy(true);
    setMsg(null);
    try {
      await fn();
      await reload();
      if (alsoTemplates) onTemplatesChanged(); // item counts on the library rows
      setMsg({ ok: true, text: okText });
    } catch (err) {
      setMsg({ ok: false, text: err instanceof Error ? err.message : "Update failed." });
    } finally {
      setBusy(false);
    }
  }

  const items = detail?.items ?? [];

  function submitAdd(e: FormEvent) {
    e.preventDefault();
    if (!addDraft.label.trim()) {
      setMsg({ ok: false, text: "Item label is required." });
      return;
    }
    void run(async () => {
      await checklist.addInspectionItem(templateId, { ...addDraft, seq: addDraft.seq ?? nextSeq(items) });
      setAddDraft(EMPTY_ITEM);
    }, "Item added.", true);
  }

  function startEdit(it: checklist.DefaultItem) {
    setEditingId(it.id);
    setEditDraft(itemInputFromRow(it));
  }

  function submitEdit(e: FormEvent) {
    e.preventDefault();
    if (editingId === null) return;
    if (!editDraft.label.trim()) {
      setMsg({ ok: false, text: "Item label is required." });
      return;
    }
    const id = editingId;
    void run(async () => {
      await checklist.editInspectionItem(templateId, id, editDraft);
      setEditingId(null);
    }, "Item updated.");
  }

  function move(index: number, dir: -1 | 1) {
    const plan = planRenumber(items, index, dir);
    if (plan.length === 0) return;
    void run(async () => {
      for (const p of plan) {
        await checklist.editInspectionItem(templateId, p.row.id, { ...itemInputFromRow(p.row), seq: p.seq });
      }
    }, "Order updated.");
  }

  return (
    <div className="checklist-split">
      {/* ── BUILD PANE — the item editor ─────────────────────────────────────── */}
      <div className="checklist-split__pane">
        <section className="card form-editor__section" aria-label="Checklist items">
          <div className="form-editor__section-head">
            <span className="form-editor__section-type">Items</span>
            {!loading && items.length > 0 && (
              <span className="forms-mgr__count">{items.length}</span>
            )}
          </div>
          <MsgLine msg={msg} />
          {loading ? (
            <div className="muted">Loading items…</div>
          ) : (
            <>
              {items.length === 0 ? (
                <div className="dash-unavail">This checklist has no items yet — now add its items below.</div>
              ) : (
                <ul className="form-editor__items checklist-items" aria-label="Checklist items list">
                  {items.map((it, idx) => (
                    <li key={it.id}>
                      {editingId === it.id ? (
                        <div className="checklist-item checklist-item--editing">
                          <ChecklistItemForm
                            label="Edit item"
                            draft={editDraft}
                            onChange={setEditDraft}
                            onSubmit={submitEdit}
                            busy={busy}
                            submitLabel="Save"
                            onCancel={() => setEditingId(null)}
                          />
                        </div>
                      ) : (
                        <div className="checklist-item">
                          <div className="checklist-item__main">
                            <span className="checklist-item__label">{it.label}</span>
                            <span className="checklist-item__meta">{itemMetaLabel(it)}</span>
                          </div>
                          <div className="checklist-item__controls">
                            <button
                              type="button"
                              className="btn btn--secondary form-editor__icon-btn"
                              aria-label={`Move ${it.label ?? `item ${it.id}`} up`}
                              disabled={busy || idx === 0}
                              onClick={() => move(idx, -1)}
                            >
                              ↑
                            </button>
                            <button
                              type="button"
                              className="btn btn--secondary form-editor__icon-btn"
                              aria-label={`Move ${it.label ?? `item ${it.id}`} down`}
                              disabled={busy || idx === items.length - 1}
                              onClick={() => move(idx, 1)}
                            >
                              ↓
                            </button>
                            <button
                              type="button"
                              className="btn btn--edit"
                              aria-label={`Edit ${it.label ?? `item ${it.id}`}`}
                              disabled={busy}
                              onClick={() => startEdit(it)}
                            >
                              Edit
                            </button>
                            <ConfirmDelete
                              actionLabel="Remove"
                              ariaLabel={`Remove ${it.label ?? `item ${it.id}`}`}
                              copy={`Remove “${it.label ?? `item ${it.id}`}” from this checklist? Already-assigned copies keep their snapshot.`}
                              busy={busy}
                              onConfirm={() =>
                                run(() => checklist.deleteInspectionItem(templateId, it.id), "Item removed.", true)
                              }
                            />
                          </div>
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              )}
              <div className="checklist-item checklist-item--add">
                <ChecklistItemForm
                  label="Add item"
                  draft={addDraft}
                  onChange={setAddDraft}
                  onSubmit={submitAdd}
                  busy={busy}
                  submitLabel="Add item"
                />
              </div>
            </>
          )}
        </section>
      </div>

      {/* ── PREVIEW PANE — always-on assignee preview ────────────────────────── */}
      <div className="checklist-split__pane">
        <h3 className="form-editor__sub-heading">Preview as assignee</h3>
        <div className="card forms-mgr__preview" aria-label="Assignee preview panel">
          <p className="muted checklist-preview__note">
            Read-only preview — this is how the checklist renders for the assignee. Controls are disabled.
          </p>
          {loading ? (
            <div className="muted">Loading items…</div>
          ) : items.length === 0 ? (
            <div className="dash-unavail">Nothing to preview yet — add an item to see it here.</div>
          ) : (
            <ul className="dash-tasklist" aria-label="Assignee preview">
              {items.map((it) => (
                <li key={it.id}>
                  <ChecklistItemRow
                    item={previewState(it)}
                    busy={true}
                    canOpenForm={false}
                    onComplete={NOOP}
                    onUncomplete={NOOP}
                    onRecordCount={NOOP}
                    onOpenForm={NOOP}
                  />
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}

// The device's local calendar date as 'YYYY-MM-DD' (the same shape instance_date carries) — used
// for the overdue check on the assignments list. Local, not UTC: an admin looks at this in Pacific
// working hours and "overdue" must not flip at 5pm because UTC rolled over.
function localTodayISO(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

// The persistent post-assign confirmation (replaces the old transient banner — an admin who looks
// up from their phone mid-task must still be able to see WHAT they just assigned and to WHOM).
interface AssignConfirmation {
  assignee: string;
  title: string;
  job: string | null;
  due: string | null;
  itemCount: number;
  // (#16) When set, this was a RECURRING define — `due` holds the anchor date, `cadence` the cadence,
  // and `recurringCreated` how many instances materialized immediately (0 for a future anchor).
  cadence: checklist.RecurrenceCadence | null;
  recurringCreated: number;
}

// (#16) Human labels for the cadence select + confirmation copy.
const CADENCE_OPTIONS: { value: checklist.RecurrenceCadence; label: string }[] = [
  { value: "daily", label: "Daily" },
  { value: "weekly", label: "Weekly" },
  { value: "biweekly", label: "Every 2 weeks" },
  { value: "monthly", label: "Monthly" },
];
const CADENCE_LABEL: Record<string, string> = Object.fromEntries(CADENCE_OPTIONS.map((o) => [o.value, o.label]));

// ── Assign control (R5 — the client halves of the R1 assign-time 422s) ────────────────────────────
// The Worker independently rejects all four stuck-assignment classes (R1); this form makes them
// unreachable from the UI: empty template → disabled "(no items yet)"; form-linked w/o job+date →
// job + due date flip to REQUIRED; unknown form code → not producible here (catalog select, R4);
// duplicate double-tap → busy-guard + FULL reset after success + the persistent confirmation card.
function AssignForm({
  templates,
  onAssigned,
}: {
  templates: checklist.InspectionTemplate[];
  onAssigned?: () => void;
}) {
  const [people, setPeople] = useState<PersonnelRow[]>([]);
  const [jobs, setJobs] = useState<JobRow[]>([]);
  const [pickersError, setPickersError] = useState(false);
  const [templateId, setTemplateId] = useState<string>("");
  const [assignee, setAssignee] = useState<string>("");
  const [jobId, setJobId] = useState<string>("");
  const [dueDate, setDueDate] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<Msg | null>(null);
  const [detail, setDetail] = useState<checklist.InspectionDetail | null>(null);
  const [detailWarn, setDetailWarn] = useState(false);
  const [confirmation, setConfirmation] = useState<AssignConfirmation | null>(null);
  // (#16) Recurring — only offered when the feature is live server-side (SessionUser flag). When on,
  // the single date field is the ANCHOR ("generates off of"), and job + anchor become required.
  const { user } = useAuth();
  const recurringEnabled = user?.recurring_checklists_enabled ?? false;
  const [recurring, setRecurring] = useState(false);
  const [cadence, setCadence] = useState<checklist.RecurrenceCadence>("daily");

  // VERIFIED (R5): POST /checklist/assign requires an ACTIVE personnel row only — a portal login is
  // NOT required. So the FULL active roster is offered (fetchFullRoster pages the cursor to
  // exhaustion). Never silent: a load failure renders an error with Retry, not an empty picker.
  async function loadPickers() {
    setPickersError(false);
    try {
      const [roster, jobsRes] = await Promise.all([checklist.fetchFullRoster(), fetchJobList("active")]);
      setPeople(roster);
      setJobs(jobsRes.jobs);
    } catch {
      setPickersError(true);
    }
  }
  useEffect(() => {
    void loadPickers();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // On template selection, fetch its detail: form-bearing items make job + due date REQUIRED (the
  // client half of the R1 'job_and_date_required' 422). Best-effort — a failed detail fetch warns
  // (with Retry) but never blocks the form; the Worker re-validates on assign regardless.
  function loadDetail(tid: number) {
    setDetailWarn(false);
    checklist
      .fetchInspectionTemplate(tid)
      .then((d) => setDetail((cur) => (String(tid) === templateIdRef.current ? d : cur)))
      .catch(() => setDetailWarn(true));
  }
  // Ref mirror so a slow detail response for a PREVIOUS selection can't clobber the current one.
  const templateIdRef = useRef(templateId);
  useEffect(() => {
    templateIdRef.current = templateId;
    setDetail(null);
    setDetailWarn(false);
    const tid = Number(templateId);
    if (Number.isInteger(tid) && tid > 0) loadDetail(tid);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [templateId]);

  const needsJobDate = (detail?.items ?? []).some((i) => isFormBearing(i.item_type));
  // (#16) A recurrence is per-job and needs an anchor, so job + date are ALWAYS required when
  // recurring — on top of the R1 form-bearing rule.
  const jobRequired = recurring || needsJobDate;
  const dateRequired = recurring || needsJobDate;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    const tid = Number(templateId);
    const aid = Number(assignee);
    if (!Number.isInteger(tid) || tid <= 0) {
      setMsg({ ok: false, text: "Pick a checklist." });
      return;
    }
    if (!Number.isInteger(aid) || aid <= 0) {
      setMsg({ ok: false, text: "Pick a person." });
      return;
    }
    // (#16) Recurring client-half of the server 422/400s — a recurrence needs a job + an anchor date.
    if (recurring && (jobId === "" || dueDate === "")) {
      setMsg({ ok: false, text: "A recurring checklist needs a job and a start (anchor) date." });
      return;
    }
    // Client half of the R1 server 422 — same rule, caught BEFORE the request with inline copy.
    if (!recurring && needsJobDate && (jobId === "" || dueDate === "")) {
      setMsg({
        ok: false,
        text: "Pick a job and a due date — this checklist auto-checks from filed forms, so it needs both.",
      });
      return;
    }
    setBusy(true);
    setMsg(null);
    try {
      const input: checklist.AssignInput = { template_id: tid, assignee_personnel_id: aid };
      if (jobId) input.job_id = jobId;
      if (recurring) {
        // The date field is the ANCHOR when recurring; never also send due_date (mutually exclusive).
        input.recurrence = { cadence, anchor_date: dueDate };
      } else if (dueDate) {
        input.due_date = dueDate;
      }
      const res = await checklist.assignInspection(input);
      const person = people.find((p) => p.id === aid);
      const tpl = templates.find((t) => t.id === tid);
      const job = jobId ? jobs.find((j) => j.job_id === jobId) : undefined;
      setConfirmation({
        assignee: person?.name ?? `person #${aid}`,
        title: tpl?.title ?? `checklist ${tid}`,
        job: jobId ? (job?.project_name ?? jobId) : null,
        due: dueDate || null,
        itemCount: !recurring && "item_count" in res ? res.item_count : 0,
        cadence: recurring ? cadence : null,
        recurringCreated: recurring && "instances_created" in res ? res.instances_created : 0,
      });
      // FULL reset (not just the date) — a repeat assign must be a deliberate fresh selection, so a
      // double-tap after success can't silently create a duplicate instance/recurrence.
      setTemplateId("");
      setAssignee("");
      setJobId("");
      setDueDate("");
      setRecurring(false);
      setCadence("daily");
      setDetail(null);
      onAssigned?.();
    } catch (err) {
      // R1: user copy comes from errorCopy.ts via err.message — never duplicated in pages.
      setMsg({ ok: false, text: err instanceof Error ? err.message : "Assign failed." });
    } finally {
      setBusy(false);
    }
  }

  // Inactive templates are retired — not assignable, so not offered. Empty (0-item) templates stay
  // VISIBLE but disabled "(no items yet)" — hiding them entirely would read as a missing checklist.
  const assignable = templates.filter((t) => t.active);

  return (
    <section className="card dash-section" aria-label="Assign an inspection">
      <h3 className="dash-detail__h2">Assign an inspection checklist</h3>
      <MsgLine msg={msg} />
      {pickersError && (
        <p className="banner banner--err">
          Couldn't load the people and jobs to assign to.{" "}
          <button type="button" className="btn btn--secondary" aria-label="Retry loading assign pickers" onClick={() => void loadPickers()}>
            Retry
          </button>
        </p>
      )}
      {confirmation && (
        <div className="banner banner--ok checklist-assign__confirm" role="status" aria-label="Assignment confirmation">
          {confirmation.cadence !== null ? (
            <>
              <strong>Recurring checklist set for {confirmation.assignee} ✓</strong>
              <span className="checklist-assign__confirm-sub">
                “{confirmation.title}” · {CADENCE_LABEL[confirmation.cadence]} from {confirmation.due}
                {confirmation.job !== null ? <> · {confirmation.job}</> : null} ·{" "}
                {confirmation.recurringCreated === 0
                  ? "first one starts on the anchor date"
                  : `${confirmation.recurringCreated} started so far`}
              </span>
            </>
          ) : (
            <>
              <strong>Assigned to {confirmation.assignee} ✓</strong>
              <span className="checklist-assign__confirm-sub">
                “{confirmation.title}” ({confirmation.itemCount} item{confirmation.itemCount === 1 ? "" : "s"})
                {confirmation.job !== null ? <> · {confirmation.job}</> : null}
                {confirmation.due !== null ? <> · due {confirmation.due}</> : null}
              </span>
            </>
          )}
        </div>
      )}
      <form onSubmit={submit} className="checklist-assign__grid" aria-label="Assign form">
        <label className="field">
          <span className="field__label">Checklist</span>
          <select className="field__input" aria-label="Checklist" value={templateId} onChange={(e) => setTemplateId(e.target.value)}>
            <option value="">— checklist —</option>
            {assignable.map((t) => (
              <option key={t.id} value={t.id} disabled={t.item_count === 0}>
                {t.title}
                {t.item_count === 0 ? " (no items yet)" : ""}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span className="field__label">Assign to</span>
          <select className="field__input" aria-label="Assignee" value={assignee} onChange={(e) => setAssignee(e.target.value)}>
            <option value="">— person —</option>
            {people.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
                {p.trade ? ` (${p.trade})` : ""}
                {p.current_job ? ` — on ${p.current_job_name ?? p.current_job}` : ""}
              </option>
            ))}
          </select>
        </label>
        {recurringEnabled && (
          <label className="field checklist-assign__full checklist-assign__recurring">
            <input
              type="checkbox"
              aria-label="Recurring checklist"
              checked={recurring}
              onChange={(e) => setRecurring(e.target.checked)}
            />
            <span className="field__label">
              Recurring checklist — re-assign it to this person on this job on a schedule
            </span>
          </label>
        )}
        <label className="field">
          <span className="field__label">{jobRequired ? "Job (required)" : "Job (optional)"}</span>
          <select className="field__input" aria-label="Job" value={jobId} onChange={(e) => setJobId(e.target.value)}>
            <option value="">{jobRequired ? "— pick a job —" : "— job (optional) —"}</option>
            {jobs.map((j) => (
              <option key={j.job_id} value={j.job_id}>{j.project_name ?? j.job_id}</option>
            ))}
          </select>
        </label>
        {recurring ? (
          <>
            <label className="field">
              <span className="field__label">Cadence</span>
              <select
                className="field__input"
                aria-label="Cadence"
                value={cadence}
                onChange={(e) => setCadence(e.target.value as checklist.RecurrenceCadence)}
              >
                {CADENCE_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
            </label>
            <label className="field">
              <span className="field__label">Start date (required)</span>
              <input
                className="field__input"
                type="date"
                aria-label="Start date"
                value={dueDate}
                onChange={(e) => setDueDate(e.target.value)}
              />
            </label>
          </>
        ) : (
          <label className="field">
            <span className="field__label">{dateRequired ? "Due date (required)" : "Due date (optional)"}</span>
            <input
              className="field__input"
              type="date"
              aria-label="Due date"
              value={dueDate}
              onChange={(e) => setDueDate(e.target.value)}
            />
          </label>
        )}
        <div className="checklist-assign__submit">
          <button type="submit" className="btn btn--primary" disabled={busy}>
            {recurring ? "Set recurring" : "Assign"}
          </button>
        </div>
        {recurring && (
          <p className="jha__notice checklist-assign__full">
            This spawns the checklist for the assignee every {CADENCE_LABEL[cadence].toLowerCase()} starting on the
            start date. Generation stops automatically when the job closes; you can also stop it anytime
            under “Recurring assignments” below.
          </p>
        )}
        {needsJobDate && (
          <p className="jha__notice checklist-assign__full">
            This checklist auto-checks from filed forms — it needs a job and a date so filings can be
            matched to it. The due date is the date the work must be filed by.
          </p>
        )}
        {detailWarn && (
          <p className="muted checklist-assign__full">
            Couldn't check this checklist's items — you can still assign; the server verifies.{" "}
            <button
              type="button"
              className="btn btn--secondary"
              aria-label="Retry loading checklist detail"
              onClick={() => {
                const tid = Number(templateId);
                if (Number.isInteger(tid) && tid > 0) loadDetail(tid);
              }}
            >
              Retry
            </button>
          </p>
        )}
      </form>
    </section>
  );
}

// ── Outstanding assignments (R5 — the admin list + cancel; GET /checklist/instances) ──────────────
// Every outstanding inspection assignment is listable, its progress visible, and cancellable — the
// close of the old fire-and-forget loop where a mistaken assignment was invisible and irrevocable.
// Loading ≠ empty; a fetch failure renders an error with Retry (never a lying blank).
function AssignmentsSection({ refreshKey }: { refreshKey: number }) {
  const [rows, setRows] = useState<checklist.AdminInstanceRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [filter, setFilter] = useState<"open" | "all">("open");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<Msg | null>(null);

  async function load(f: "open" | "all") {
    setLoading(true);
    setLoadError(false);
    try {
      const res = await checklist.fetchChecklistInstances(f);
      setRows(res.instances);
    } catch {
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    void load(filter);
    // refreshKey: bumped by AssignForm after a successful assign so the new row appears immediately.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter, refreshKey]);

  function cancel(row: checklist.AdminInstanceRow) {
    if (busy) return;
    setBusy(true);
    setMsg(null);
    void (async () => {
      try {
        await checklist.cancelChecklistInstance(row.id);
        await load(filter);
        setMsg({
          ok: true,
          text: `Cancelled “${row.template_title ?? `Inspection #${row.id}`}” for ${row.assignee_name ?? "the assignee"}.`,
        });
      } catch (err) {
        setMsg({ ok: false, text: err instanceof Error ? err.message : "Cancel failed." });
      } finally {
        setBusy(false);
      }
    })();
  }

  const today = localTodayISO();

  return (
    <section className="card dash-section" aria-label="Outstanding assignments">
      <div className="checklist-track__head">
        <h3 className="dash-detail__h2">Outstanding assignments</h3>
        <div className="checklist-track__filter">
          <button
            type="button"
            className={filter === "open" ? "btn btn--primary" : "btn btn--secondary"}
            aria-label="Show open assignments"
            onClick={() => setFilter("open")}
          >
            Open
          </button>
          <button
            type="button"
            className={filter === "all" ? "btn btn--primary" : "btn btn--secondary"}
            aria-label="Show all assignments"
            onClick={() => setFilter("all")}
          >
            All
          </button>
        </div>
      </div>
      <p className="dash-card__sub muted">
        Every assigned inspection checklist — who has it, which job, when it's due, and how far along
        it is. Cancel removes it from the person's Assigned inspections.
      </p>
      <MsgLine msg={msg} />
      {loading ? (
        <div className="muted">Loading assignments…</div>
      ) : loadError ? (
        <p className="banner banner--err">
          Couldn't load the assignments.{" "}
          <button type="button" className="btn btn--secondary" aria-label="Retry loading assignments" onClick={() => void load(filter)}>
            Retry
          </button>
        </p>
      ) : rows.length === 0 ? (
        <div className="dash-unavail">
          {filter === "open"
            ? "No open assignments — everything assigned has been completed (or nothing is assigned yet)."
            : "No assignments yet."}
        </div>
      ) : (
        <ul className="dash-tasklist" aria-label="Assignment rows">
          {rows.map((r) => {
            const title = r.template_title ?? `Inspection #${r.id}`;
            const who = r.assignee_name ?? "(unknown assignee)";
            const overdue = r.status === "open" && r.instance_date !== null && r.instance_date < today;
            return (
              <li key={r.id} className="checklist-track__row">
                <div className="checklist-track__row-main">
                  <strong>{title}</strong>
                  <span className="dash-card__sub"> · {who}</span>
                  {r.job_id !== null && (
                    <span className="dash-card__sub"> · {r.project_name ?? r.job_id}</span>
                  )}
                  {r.instance_date !== null && <span className="dash-card__sub"> · due {r.instance_date}</span>}{" "}
                  {overdue && <span className="dash-pill dash-pill--warn">overdue</span>}{" "}
                  <span className={r.status === "complete" ? "dash-pill dash-pill--ok" : "dash-pill"}>
                    {statusLabel(r.status)}
                  </span>
                  <span className="dash-card__sub">
                    {" "}· {r.items_done}/{r.items_total} item{r.items_total === 1 ? "" : "s"} done
                  </span>
                </div>
                <ConfirmDelete
                  actionLabel="Cancel"
                  ariaLabel={`Cancel assignment ${title} for ${who}`}
                  copy={`Cancel “${title}” for ${who}? This removes it from ${who}'s Assigned inspections — any completed items are discarded.`}
                  busy={busy}
                  onConfirm={() => cancel(r)}
                />
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

// ── Recurring assignments (#16 — the admin list + stop; GET /checklist/recurrences) ────────────────
// The visibility + revoke half of "stop generating when the assignment is deactivated": every ACTIVE
// per-job recurring generator is listable (who / which job / cadence / anchor) and stoppable. Stopping
// is non-destructive (already-spawned instances stay under Outstanding assignments — cancel those
// individually); the cron also auto-stops a recurrence when its job closes. Loading ≠ empty; a fetch
// failure renders an error with Retry (never a lying blank). Rendered only when the feature is live.
function RecurrencesSection({ refreshKey }: { refreshKey: number }) {
  const [rows, setRows] = useState<checklist.ChecklistRecurrence[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<Msg | null>(null);

  async function load() {
    setLoading(true);
    setLoadError(false);
    try {
      const res = await checklist.fetchChecklistRecurrences();
      setRows(res.recurrences);
    } catch {
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    void load();
    // refreshKey: bumped after a successful (recurring) assign so a new definition appears at once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey]);

  function stop(row: checklist.ChecklistRecurrence) {
    if (busy) return;
    setBusy(true);
    setMsg(null);
    void (async () => {
      try {
        await checklist.deactivateChecklistRecurrence(row.id);
        await load();
        setMsg({
          ok: true,
          text: `Stopped the recurring “${row.template_title ?? `checklist #${row.id}`}” for ${row.assignee_name ?? "the assignee"}. Already-created instances remain under Outstanding assignments.`,
        });
      } catch (err) {
        setMsg({ ok: false, text: err instanceof Error ? err.message : "Stop failed." });
      } finally {
        setBusy(false);
      }
    })();
  }

  return (
    <section className="card dash-section" aria-label="Recurring assignments">
      <h3 className="dash-detail__h2">Recurring assignments</h3>
      <p className="dash-card__sub muted">
        Checklists that re-assign themselves on a schedule. Stopping a recurrence ends future
        generation only — it never touches instances already created (cancel those above). A
        recurrence also stops on its own when the job closes.
      </p>
      <MsgLine msg={msg} />
      {loading ? (
        <div className="muted">Loading recurring assignments…</div>
      ) : loadError ? (
        <p className="banner banner--err">
          Couldn't load the recurring assignments.{" "}
          <button type="button" className="btn btn--secondary" aria-label="Retry loading recurring assignments" onClick={() => void load()}>
            Retry
          </button>
        </p>
      ) : rows.length === 0 ? (
        <div className="dash-unavail">No recurring assignments — nothing is set to repeat.</div>
      ) : (
        <ul className="dash-tasklist" aria-label="Recurring assignment rows">
          {rows.map((r) => {
            const title = r.template_title ?? `Checklist #${r.id}`;
            const who = r.assignee_name ?? "(unknown assignee)";
            return (
              <li key={r.id} className="checklist-track__row">
                <div className="checklist-track__row-main">
                  <strong>{title}</strong>
                  <span className="dash-card__sub"> · {who}</span>
                  {r.job_id !== null && <span className="dash-card__sub"> · {r.project_name ?? r.job_id}</span>}
                  <span className="dash-pill"> {CADENCE_LABEL[r.cadence] ?? r.cadence}</span>
                  <span className="dash-card__sub"> · from {r.anchor_date}</span>
                </div>
                <ConfirmDelete
                  actionLabel="Stop"
                  ariaLabel={`Stop recurring ${title} for ${who}`}
                  copy={`Stop the recurring “${title}” for ${who}? Future generation ends; already-created instances stay under Outstanding assignments.`}
                  busy={busy}
                  onConfirm={() => stop(r)}
                />
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

/**
 * R8 — the admin "Checklists" page (view key 'fieldops-inspections' unchanged), redesigned to the
 * Forms form-builder shape: a master-detail Inspection library (list → detail) whose detail carries
 * the lifecycle actions + a form-editor SPLIT (item editor beside a live assignee preview); then the
 * "Assign & track" band. Inspections-only since D2 (the daily content lives in the daily-report-v2
 * form definition — edit it via the form builder). cap.checklist.manage gates the Home card + every
 * call (the Worker re-gates — Invariant 2).
 */
export function FieldOpsInspections({ onBack }: { onBack: () => void }) {
  const [templates, setTemplates] = useState<checklist.InspectionTemplate[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [newTitle, setNewTitle] = useState("");
  const [filter, setFilter] = useState("");
  const [renaming, setRenaming] = useState(false);
  const [renameTitle, setRenameTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<Msg | null>(null);
  // R5: bumped after each successful assign so the Outstanding-assignments section refetches.
  const [assignmentsRefresh, setAssignmentsRefresh] = useState(0);
  // (#16) Only render the Recurring-assignments band when the feature is live server-side.
  const { user } = useAuth();
  const recurringEnabled = user?.recurring_checklists_enabled ?? false;

  async function reload() {
    try {
      const { templates: t } = await checklist.fetchInspectionTemplates();
      setTemplates(t);
    } catch {
      setMsg({ ok: false, text: "Could not load the inspection library." });
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    void reload();
  }, []);

  async function run(fn: () => Promise<unknown>, okText: string) {
    if (busy) return;
    setBusy(true);
    setMsg(null);
    try {
      await fn();
      await reload();
      setMsg({ ok: true, text: okText });
    } catch (err) {
      setMsg({ ok: false, text: err instanceof Error ? err.message : "Update failed." });
    } finally {
      setBusy(false);
    }
  }

  function create(e: FormEvent) {
    e.preventDefault();
    if (newTitle.trim() === "") {
      setMsg({ ok: false, text: "A title is required." });
      return;
    }
    void run(async () => {
      const res = await checklist.createInspectionTemplate(newTitle.trim());
      setNewTitle("");
      if (res.id) setSelectedId(res.id); // opens the new checklist in the detail pane
    }, "Checklist created — now add its items on the right.");
  }

  function startRename(t: checklist.InspectionTemplate) {
    setRenaming(true);
    setRenameTitle(t.title ?? "");
  }

  function submitRename(e: FormEvent, id: number) {
    e.preventDefault();
    if (renameTitle.trim() === "") {
      setMsg({ ok: false, text: "A title is required." });
      return;
    }
    void run(async () => {
      await checklist.editInspectionTemplate(id, { title: renameTitle.trim() });
      setRenaming(false);
    }, "Checklist renamed.");
  }

  function setActive(t: checklist.InspectionTemplate, active: boolean) {
    void run(
      () => checklist.editInspectionTemplate(t.id, { title: t.title ?? "", active }),
      active ? "Checklist reactivated — it can be assigned again." : "Checklist deactivated — it can no longer be assigned.",
    );
  }

  function remove(t: checklist.InspectionTemplate) {
    void run(async () => {
      await checklist.deleteInspectionTemplate(t.id);
      if (selectedId === t.id) setSelectedId(null);
    }, "Checklist deleted.");
  }

  const visible = filter.trim() === ""
    ? templates
    : templates.filter((t) => (t.title ?? "").toLowerCase().includes(filter.trim().toLowerCase()));
  const selected = selectedId !== null ? templates.find((t) => t.id === selectedId) ?? null : null;

  function select(t: checklist.InspectionTemplate) {
    setSelectedId((cur) => (cur === t.id ? null : t.id));
    setRenaming(false);
  }

  return (
    <PageShell onHome={onBack}>
      <div className="checklist-page">
      <h2 className="page__heading">Checklists</h2>

      <p className="dash__intro checklist-intro">
        The <strong>inspection checklists</strong> you author and assign to a manager or subcontractor
        (they appear in that person's My Tasks tab). The daily report's content is no longer a
        checklist edited here — it lives in the Daily Field Report <strong>form definition</strong>{" "}
        (edit it in Forms, the form builder). <strong>Per-job daily-form requirements</strong>{" "}
        (client-specific items rendered inside each day's Daily Report) live on each job's detail
        page in the Job Tracker.
      </p>

      <MsgLine msg={msg} />

      <div className="forms-mgr">
        {/* ── LIBRARY (master) ───────────────────────────────────────────────── */}
        <aside className="forms-mgr__list" aria-label="Inspection library">
          <form className="checklist-lib-new" onSubmit={create} aria-label="Create checklist">
            <input
              className="field__input"
              aria-label="New checklist title"
              value={newTitle}
              onChange={(e) => setNewTitle(e.target.value)}
              placeholder="New checklist title"
              maxLength={256}
            />
            <button type="submit" className="btn btn--primary checklist-lib-new__btn" disabled={busy || loading}>New +</button>
          </form>
          <h3 className="forms-mgr__heading">
            Library <span className="forms-mgr__count">{templates.length}</span>
          </h3>
          {loading ? (
            <div className="muted">Loading inspection checklists…</div>
          ) : templates.length === 0 ? (
            <div className="dash-unavail">No inspection checklists yet — create one above.</div>
          ) : (
            <>
              {templates.length > 3 && (
                <input
                  className="field__input checklist-filter"
                  aria-label="Filter checklists"
                  value={filter}
                  onChange={(e) => setFilter(e.target.value)}
                  placeholder="Filter by title"
                />
              )}
              {visible.length === 0 ? (
                <div className="muted">No checklist titles match “{filter.trim()}”.</div>
              ) : (
                <ul className="forms-mgr__items">
                  {visible.map((t) => (
                    <li key={t.id}>
                      <button
                        type="button"
                        className={`forms-mgr__item${selectedId === t.id ? " forms-mgr__item--active" : ""}`}
                        aria-current={selectedId === t.id}
                        aria-label={`Edit ${t.title}`}
                        onClick={() => select(t)}
                      >
                        <span className="forms-mgr__item-label">
                          {t.title}
                          {!t.active && <span className="dash-pill dash-pill--warn checklist-inactive-pill">inactive — not assignable</span>}
                        </span>
                        <span className="forms-mgr__item-parent">
                          {t.item_count} item{t.item_count === 1 ? "" : "s"} · created{" "}
                          {new Date(t.created_at * 1000).toLocaleDateString()}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
        </aside>

        {/* ── DETAIL (selected checklist) ────────────────────────────────────── */}
        <section aria-label="Checklist detail">
          {selected === null ? (
            <div className="dash-empty">
              {templates.length === 0
                ? "Create a checklist to start authoring its items."
                : "Select a checklist on the left to edit its items, or create a new one."}
            </div>
          ) : (
            <>
              <header className="forms-mgr__meta">
                {renaming ? (
                  <form className="checklist-rename" onSubmit={(e) => submitRename(e, selected.id)} aria-label={`Rename ${selected.title}`}>
                    <input
                      className="field__input"
                      aria-label={`Rename ${selected.title} title`}
                      value={renameTitle}
                      onChange={(e) => setRenameTitle(e.target.value)}
                      maxLength={256}
                    />
                    <button type="submit" className="btn btn--primary" disabled={busy}>Save</button>
                    <button type="button" className="btn btn--secondary" onClick={() => setRenaming(false)}>Cancel</button>
                  </form>
                ) : (
                  <h2 className="page__heading">{selected.title}</h2>
                )}
                <dl className="forms-mgr__meta-grid">
                  <div><dt>Items</dt><dd>{selected.item_count}</dd></div>
                  <div><dt>Created</dt><dd>{new Date(selected.created_at * 1000).toLocaleDateString()}</dd></div>
                  <div>
                    <dt>Status</dt>
                    <dd>{selected.active ? "Active" : "Inactive — not assignable"}</dd>
                  </div>
                </dl>
                {!renaming && (
                  <div className="checklist-detail__actions">
                    <button
                      type="button"
                      className="btn btn--edit"
                      aria-label={`Rename ${selected.title}`}
                      disabled={busy}
                      onClick={() => startRename(selected)}
                    >
                      Rename
                    </button>
                    {selected.active ? (
                      <button
                        type="button"
                        className="btn btn--secondary"
                        aria-label={`Deactivate ${selected.title}`}
                        disabled={busy}
                        onClick={() => setActive(selected, false)}
                      >
                        Deactivate
                      </button>
                    ) : (
                      <button
                        type="button"
                        className="btn btn--secondary"
                        aria-label={`Reactivate ${selected.title}`}
                        disabled={busy}
                        onClick={() => setActive(selected, true)}
                      >
                        Reactivate
                      </button>
                    )}
                    <ConfirmDelete
                      actionLabel="Delete"
                      ariaLabel={`Delete ${selected.title}`}
                      copy={`Delete “${selected.title}” and its ${selected.item_count} item${selected.item_count === 1 ? "" : "s"}? Any outstanding assigned copies keep their snapshot. Prefer Deactivate to retire it without deleting.`}
                      busy={busy}
                      onConfirm={() => remove(selected)}
                    />
                  </div>
                )}
              </header>

              <TemplateItemsEditor template={selected} onTemplatesChanged={() => void reload()} />
            </>
          )}
        </section>
      </div>

      {/* ── ASSIGN & TRACK band ──────────────────────────────────────────────── */}
      <AssignForm templates={templates} onAssigned={() => setAssignmentsRefresh((k) => k + 1)} />
      <AssignmentsSection refreshKey={assignmentsRefresh} />
      {recurringEnabled && <RecurrencesSection refreshKey={assignmentsRefresh} />}
      </div>
    </PageShell>
  );
}
