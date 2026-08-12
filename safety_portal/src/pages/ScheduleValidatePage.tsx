import { useCallback, useEffect, useMemo, useState } from "react";
import * as api from "../lib/fieldops_schedules";
import type { ScheduleCommitRow, ScheduleListRow } from "../lib/fieldops_schedules";
import {
  deriveGrid,
  draftProblem,
  toDurationDays,
  toPercent,
  type DerivedGridItem,
} from "../lib/schedule_grid";
import { errorText } from "../lib/errorCopy";

// ─────────────────────────────────────────────────────────────────────────────
// Schedule validate screen (ADR-0006 PR-4) — the human half of "the parser proposes,
// the human disposes".
//
// A SUB-FACE of JobSchedulePage (props + onClose, no PageShell — the ManifestValidatePage
// pattern), mounted `key={scheduleId}` so switching schedules remounts rather than leaking
// the previous document's edits into the next.
//
// WHY THIS SCREEN EXISTS. The corpus PDFs carry NO text layer, so every cell here came out
// of Apple Vision OCR — which misreads digits AT CONFIDENCE 1.0 ('12/01/25' → '72/01/25' is
// real). No automated check can catch a wrong-but-self-consistent read; the SIDE-BY-SIDE
// SOURCE PAGE is the single fidelity control (decision 2), so:
//
//   • the rendered source page sits beside the editable grid;
//   • consistency flags (implausible year, finish-before-start, …) point the human at
//     LIKELY misreads — they warn, they never auto-correct (§4 no-invented-field-data);
//   • the PREVIEW-EVIDENCE GATE: commit stays disabled until at least one source page has
//     actually rendered (an <img> onLoad fired) OR the office explicitly acknowledges
//     reviewing the PDF elsewhere — the estimate lane's no_preview_verified pattern.
//     UX-ONLY (both faces; a Worker-side acknowledgment column stays future work).
//
// This face is the FIRST-IMPORT commit (the diff's degenerate arm). A job that already
// has a task list is routed by JobSchedulePage to the RECONCILE face instead
// (ScheduleReconcilePage, PR-6); if a task list appears between routing and the plan
// (a race), the non-degenerate plan here keeps commit disabled and points at the
// reconcile screen — this face collects no resolutions, so committing from it would
// hit the Worker's blocking-removal / ambiguity refusals anyway.
// ─────────────────────────────────────────────────────────────────────────────

/** `schedule_parse` consistency flag → what a human should do about it. */
const FLAG_COPY: Record<string, string> = {
  implausible_year: "a date's year looks wrong — likely an OCR digit misread; check the source page",
  unparseable_date: "a date couldn't be read — fix it against the source page",
  finish_before_start: "finish is before start — one of the dates is misread",
  percent_out_of_range: "the % read is outside 0–100 — check the source page",
  missing_dates: "no dates were read for this task",
  percent_conflict: "the table % and the Gantt-bar label disagree — check which is right",
  low_confidence: "the OCR read this row with low confidence — check every cell",
};

/** One editable data row of the proposal. Strings while typing; validated at plan/commit. */
type TaskDraft = {
  row_index: number;
  keep: boolean;
  name: string;
  section: string;
  duration: string; // tolerates the export's "137d" spelling
  start: string;
  finish: string;
  percent: string;
  predecessors: string;
  milestone: boolean;
  contractMilestone: boolean;
  delivery: boolean;
  flags: string[];
};

/** A task the office adds by hand — the Tier-3 manual floor (decision 3). */
type AddedTask = {
  key: string;
  name: string;
  section: string;
  duration: string;
  start: string;
  finish: string;
  percent: string;
};

/** Display order interleaves section DIVIDERS with the editable tasks (the shared
 *  schedule_grid derivation's item shape). */
type DisplayItem = DerivedGridItem;

function errText(e: unknown, fallback: string): string {
  if (e && typeof e === "object" && "code" in e) {
    const code = (e as { code: string | null }).code;
    if (code) return errorText(code);
  }
  return e instanceof Error && e.message ? e.message : fallback;
}

export function ScheduleValidatePage({
  scheduleId,
  onClose,
}: {
  scheduleId: number;
  /** Leave the screen. `committed` tells the parent to reload its task list — a commit
   *  authors the living task list, so the page behind this one is stale the moment we finish. */
  onClose: (notice?: { ok: boolean; text: string }, committed?: boolean) => void;
}) {
  const [schedule, setSchedule] = useState<
    (ScheduleListRow & { column_map_json: string | null; parse_notes: string | null }) | null
  >(null);
  const [previewPages, setPreviewPages] = useState<number[]>([]);
  const [notes, setNotes] = useState<string[]>([]);
  const [drafts, setDrafts] = useState<Record<number, TaskDraft>>({});
  const [display, setDisplay] = useState<DisplayItem[] | null>(null);
  const [added, setAdded] = useState<AddedTask[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [plan, setPlan] = useState<api.SchedulePlanResponse | null>(null);
  const [busy, setBusy] = useState<null | "plan" | "commit" | "discard">(null);
  const [progress, setProgress] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const [page, setPage] = useState(1);
  const [previewSrc, setPreviewSrc] = useState<string | null>(null);
  // The PREVIEW-EVIDENCE GATE's two inputs (see the module header).
  const [previewLoaded, setPreviewLoaded] = useState(false);
  const [reviewedElsewhere, setReviewedElsewhere] = useState(false);

  const load = useCallback(() => {
    setLoadError(null);
    Promise.all([api.fetchSchedule(scheduleId), api.fetchAllScheduleRows(scheduleId)])
      .then(([d, rows]) => {
        // Seed the human decisions FROM the parser's proposal — the "proposes" half.
        // The derivation (running sections, column map, milestone/delivery pre-checks)
        // is SHARED with the reconcile face (src/lib/schedule_grid.ts) so the two
        // sub-faces cannot disagree about what a grid row means. Proposals only:
        // every toggle is editable, nothing auto-commits (§4). Contract-milestone has
        // no OCR signal — always a human decision, seeded false here.
        const grid = deriveGrid(rows, d.schedule.column_map_json);
        const seeded: Record<number, TaskDraft> = {};
        for (const [rowIndex, task] of grid.tasks) {
          seeded[rowIndex] = { ...task, keep: true, contractMilestone: false };
        }
        setSchedule(d.schedule);
        setPreviewPages(d.preview_pages);
        setNotes((d.schedule.parse_notes ?? "").split("\n").filter(Boolean));
        setDrafts(seeded);
        setDisplay(grid.items);
        if (d.preview_pages.length > 0) setPage(d.preview_pages[0]);
      })
      .catch((e) => setLoadError(errText(e, "Could not load this schedule.")));
  }, [scheduleId]);

  useEffect(load, [load]);

  // Preview: fetch on page change. `key={page}` on the <img> is load-bearing — without it
  // React reuses the element and onLoad never re-fires (the evidence gate would starve).
  useEffect(() => {
    let cancelled = false;
    if (!previewPages.includes(page)) {
      setPreviewSrc(null);
      return;
    }
    api
      .fetchSchedulePreview(scheduleId, page)
      .then((p) => {
        if (!cancelled) setPreviewSrc(`data:image/png;base64,${p.png_b64}`);
      })
      .catch(() => {
        if (!cancelled) setPreviewSrc(null);
      });
    return () => {
      cancelled = true;
    };
  }, [scheduleId, page, previewPages]);

  const taskDrafts = useMemo(
    () => Object.values(drafts).sort((a, b) => a.row_index - b.row_index),
    [drafts],
  );
  const keptDrafts = useMemo(() => taskDrafts.filter((d) => d.keep), [taskDrafts]);
  const addedBase = useMemo(
    () => Math.max(0, ...taskDrafts.map((d) => d.row_index)) + 1,
    [taskDrafts],
  );

  /** The current decisions → the rows a commit would author. ONE derivation — Preview and
   *  Commit both read it, so what you previewed IS what commits. */
  const resolvedRows = useMemo((): ScheduleCommitRow[] => {
    const out: ScheduleCommitRow[] = [];
    for (const d of keptDrafts) {
      out.push({
        source_row_index: d.row_index,
        kind: "data",
        name: d.name.trim(),
        section: d.section.trim() || null,
        duration_days: toDurationDays(d.duration),
        start_date: d.start.trim() || null,
        finish_date: d.finish.trim() || null,
        // Empty % commits as 0 — the schema's own floor, shown here before commit.
        percent_done: toPercent(d.percent) ?? 0,
        is_milestone: d.milestone,
        is_contract_milestone: d.contractMilestone,
        is_delivery: d.delivery,
        predecessors_raw: d.predecessors.trim() || null,
      });
    }
    // Hand-added tasks ride after the document's own, keyed off the highest source index
    // so their watermark positions never collide with a real row's.
    added.forEach((a, i) => {
      out.push({
        source_row_index: addedBase + i,
        kind: "data",
        name: a.name.trim(),
        section: a.section.trim() || null,
        duration_days: toDurationDays(a.duration),
        start_date: a.start.trim() || null,
        finish_date: a.finish.trim() || null,
        percent_done: toPercent(a.percent) ?? 0,
        is_milestone: false,
        is_contract_milestone: false,
        is_delivery: a.section.trim().toLowerCase() === "deliveries",
        predecessors_raw: null,
      });
    });
    return out;
  }, [keptDrafts, added, addedBase]);

  /** Rows the Worker WOULD refuse — named per row with the reason, blocking the commit,
   *  so one shredded OCR cell can't turn into a whole-commit 400 mid-import. */
  const problems = useMemo(() => {
    const out: { row_index: number; label: string; problem: string; addedKey?: string }[] = [];
    for (const d of keptDrafts) {
      const p = draftProblem(d);
      if (p) out.push({ row_index: d.row_index, label: d.name.trim() || `row ${d.row_index}`, problem: p });
    }
    added.forEach((a, i) => {
      const p = draftProblem({ ...a, predecessors: "" });
      if (p) out.push({ row_index: addedBase + i, label: a.name.trim() || "added task", problem: p, addedKey: a.key });
    });
    return out;
  }, [keptDrafts, added, addedBase]);

  const status = schedule?.status;
  const committable = status === "parsed" || status === "committing";
  // The evidence gate (module header): a rendered source page, or the explicit
  // "reviewed elsewhere" acknowledgment. UX-only (module header).
  const previewEvidence = previewLoaded || reviewedElsewhere;

  function setDraft(rowIndex: number, patch: Partial<TaskDraft>) {
    setDrafts((s) => ({ ...s, [rowIndex]: { ...s[rowIndex], ...patch } }));
    setPlan(null); // a changed set invalidates the dry run
  }

  async function runPlan() {
    if (busy) return;
    setBusy("plan");
    setMsg(null);
    try {
      const p = await api.planSchedule(scheduleId, resolvedRows);
      setPlan(p);
    } catch (e) {
      setMsg({ ok: false, text: errText(e, "Could not preview the import.") });
    } finally {
      setBusy(null);
    }
  }

  async function runCommit() {
    if (busy) return;
    setBusy("commit");
    setMsg(null);
    setProgress(null);
    try {
      const res = await api.commitAllSchedule(scheduleId, resolvedRows, {
        onProgress: (landed, total) => setProgress(`${landed} of ${total} tasks…`),
      });
      onClose(
        {
          ok: true,
          text: `Imported ${res.inserted} task${res.inserted === 1 ? "" : "s"} — this is now the job's governing schedule.`,
        },
        true,
      );
    } catch (e) {
      // HONEST failure copy: the commit is PAGED, so earlier pages may already be on the
      // task list — never claim nothing landed.
      setMsg({
        ok: false,
        text: errText(
          e,
          "The import stopped part-way. Tasks from pages that already landed ARE on the list — check the Schedule page before importing again, or discard this upload to stop here.",
        ),
      });
      setBusy(null);
      setProgress(null);
    }
  }

  async function runDiscard() {
    if (busy) return;
    setBusy("discard");
    setMsg(null);
    try {
      await api.discardSchedule(scheduleId);
      onClose(
        {
          ok: true,
          text:
            schedule?.status === "committing"
              ? "Schedule discarded. Tasks the interrupted import had already created were removed with it."
              : "Schedule discarded.",
        },
        schedule?.status === "committing",
      );
    } catch (e) {
      setMsg({ ok: false, text: errText(e, "Could not discard this schedule.") });
      setBusy(null);
    }
  }

  if (loadError) {
    return (
      <section className="card dash-section" aria-label="Schedule load error">
        <p className="dash-error">{loadError}</p>
        <button type="button" className="btn btn--secondary" onClick={() => onClose()}>
          ← Back to schedule
        </button>
      </section>
    );
  }
  if (!schedule || !display) {
    return (
      <section className="card dash-section" aria-label="Loading schedule">
        <p className="dash__intro">Loading the schedule…</p>
      </section>
    );
  }

  return (
    <>
      <div className="dash-row">
        <button type="button" className="btn btn--secondary" onClick={() => onClose()}>
          ← Back to schedule
        </button>
        <span className="dash-pill">{schedule.status}</span>
      </div>
      <h3 className="page__heading">{schedule.filename}</h3>

      {msg ? (
        <p className={msg.ok ? "dash-ok" : "dash-error"} role="status">
          {msg.text}
        </p>
      ) : null}

      {!committable ? (
        <section className="card dash-section" aria-label="Schedule not ready">
          <p className="dash__intro">
            {schedule.status === "refused"
              ? `This schedule was refused${schedule.detail ? ` (${schedule.detail})` : ""} — re-export it and upload again.`
              : schedule.status === "committed"
                ? "This upload is the job's governing schedule — it has already been imported."
                : schedule.status === "superseded"
                  ? "A later revision replaced this upload — it is kept as revision history."
                  : schedule.status === "discarded"
                    ? "This schedule was discarded."
                    : "This schedule is still being read. Refresh in a moment."}
          </p>
        </section>
      ) : null}

      {committable ? (
        // The split lives in CSS now (schedule-report.css), where a media query can reach
        // it. It used to be an inline grid-template-columns, which one structurally cannot:
        // the 1 : 1.6 ratio held at 320px as firmly as at 3440px, so the source preview —
        // this screen's only defence against an OCR misread — rendered about 100px wide on
        // a phone. It now stacks below 1024px and sticks beside the grid above it.
        <div className="schedule-validate__split">
          {/* ── LEFT: the source — the ONLY fidelity control this lane has ─────────── */}
          <section className="card dash-section" style={{ minWidth: 0 }} aria-label="Source document">
            <h4>Source</h4>
            {previewPages.length === 0 ? (
              <>
                <p className="dash__intro">
                  No page preview came back for this file. The grid below is an OCR read of a
                  document you cannot see here — check it against your own copy of the PDF
                  before importing.
                </p>
                <label className="dash-row">
                  <input
                    type="checkbox"
                    checked={reviewedElsewhere}
                    aria-label="I reviewed the source PDF elsewhere"
                    onChange={(e) => setReviewedElsewhere(e.target.checked)}
                  />{" "}
                  I reviewed the source PDF elsewhere
                </label>
              </>
            ) : (
              <>
                <div className="dash-row" role="tablist" aria-label="Source pages">
                  {previewPages.map((p) => (
                    <button
                      key={p}
                      type="button"
                      role="tab"
                      aria-selected={p === page}
                      className={`btn btn--sm ${p === page ? "btn--primary" : "btn--secondary"}`}
                      onClick={() => setPage(p)}
                    >
                      Page {p}
                    </button>
                  ))}
                </div>
                {previewSrc ? (
                  <img
                    key={page}
                    src={previewSrc}
                    alt={`${schedule.filename} page ${page}`}
                    onLoad={() => setPreviewLoaded(true)}
                    className="sched-check__page"
                  />
                ) : (
                  <p className="dash__intro">Loading page {page}…</p>
                )}
                {!previewLoaded ? (
                  <label className="dash-row">
                    <input
                      type="checkbox"
                      checked={reviewedElsewhere}
                      aria-label="I reviewed the source PDF elsewhere"
                      onChange={(e) => setReviewedElsewhere(e.target.checked)}
                    />{" "}
                    I reviewed the source PDF elsewhere
                  </label>
                ) : null}
              </>
            )}

            {notes.length > 0 ? (
              <>
                <h4>What the reader found</h4>
                <ul aria-label="Parser notes">
                  {notes.map((n) => (
                    <li key={n}>{n}</li>
                  ))}
                </ul>
              </>
            ) : null}
          </section>

          {/* ── RIGHT: the editable proposal + the commit ──────────────────────────── */}
          <div style={{ minWidth: 0, display: "grid", gap: "1rem" }}>
            <section className="card dash-section" aria-label="Proposed tasks">
              <h4>
                Tasks ({resolvedRows.length} selected)
              </h4>
              <p className="dash__intro">
                The reader proposed these from the PDF. Dates it misread arrive looking
                confident — check every ⚠ row against the source page. Section rows become
                each task's section, not tasks of their own.
              </p>
              {problems.length > 0 ? (
                <div className="dash-error" role="alert">
                  <p>
                    {problems.length} selected row{problems.length === 1 ? " needs" : "s need"} fixing
                    before the import can run:
                  </p>
                  <ul aria-label="Rows the import would refuse">
                    {problems.slice(0, 20).map((p) => (
                      <li key={p.row_index}>
                        Row {p.row_index} ({p.label}) — {p.problem}
                      </li>
                    ))}
                    {problems.length > 20 ? <li>…and {problems.length - 20} more</li> : null}
                  </ul>
                </div>
              ) : null}
              <div style={{ overflowX: "auto", maxHeight: "30rem" }}>
                <table className="dash-table">
                  <thead>
                    <tr>
                      <th scope="col">Import</th>
                      <th scope="col">#</th>
                      <th scope="col">Task</th>
                      <th scope="col">Section</th>
                      <th scope="col">Duration</th>
                      <th scope="col">Start</th>
                      <th scope="col">Finish</th>
                      <th scope="col">%</th>
                      <th scope="col">Preds</th>
                      <th scope="col">Marks</th>
                    </tr>
                  </thead>
                  <tbody>
                    {display.map((item) => {
                      if (item.type === "section") {
                        return (
                          <tr key={item.row_index}>
                            <td colSpan={10}>
                              <strong aria-label={`Section ${item.label}`}>— {item.label} —</strong>
                            </td>
                          </tr>
                        );
                      }
                      if (item.type === "other") {
                        return (
                          <tr key={item.row_index}>
                            <td colSpan={10} className="dash-unavail">
                              ({item.kind}) {item.text}
                            </td>
                          </tr>
                        );
                      }
                      const d = drafts[item.row_index];
                      if (!d) return null;
                      return (
                        <tr key={item.row_index}>
                          <td>
                            <input
                              type="checkbox"
                              checked={d.keep}
                              aria-label={`Import source row ${d.row_index}`}
                              onChange={(e) => setDraft(d.row_index, { keep: e.target.checked })}
                            />
                          </td>
                          <td>
                            {d.row_index}
                            {d.flags.length > 0 ? (
                              <span
                                role="img"
                                aria-label={`Row ${d.row_index} warnings: ${d.flags.map((f) => FLAG_COPY[f] ?? f).join("; ")}`}
                                title={d.flags.map((f) => FLAG_COPY[f] ?? f).join("; ")}
                              >
                                {" "}⚠
                              </span>
                            ) : null}
                          </td>
                          <td>
                            <input
                              type="text"
                              value={d.name}
                              aria-label={`Row ${d.row_index} task name`}
                              onChange={(e) => setDraft(d.row_index, { name: e.target.value })}
                            />
                          </td>
                          <td>
                            <input
                              type="text"
                              value={d.section}
                              size={12}
                              aria-label={`Row ${d.row_index} section`}
                              onChange={(e) => setDraft(d.row_index, { section: e.target.value })}
                            />
                          </td>
                          <td>
                            <input
                              type="text"
                              value={d.duration}
                              size={5}
                              aria-label={`Row ${d.row_index} duration`}
                              onChange={(e) => setDraft(d.row_index, { duration: e.target.value })}
                            />
                          </td>
                          <td>
                            <input
                              type="text"
                              value={d.start}
                              size={10}
                              aria-label={`Row ${d.row_index} start date`}
                              onChange={(e) => setDraft(d.row_index, { start: e.target.value })}
                            />
                          </td>
                          <td>
                            <input
                              type="text"
                              value={d.finish}
                              size={10}
                              aria-label={`Row ${d.row_index} finish date`}
                              onChange={(e) => setDraft(d.row_index, { finish: e.target.value })}
                            />
                          </td>
                          <td>
                            <input
                              type="text"
                              value={d.percent}
                              size={4}
                              aria-label={`Row ${d.row_index} percent done`}
                              onChange={(e) => setDraft(d.row_index, { percent: e.target.value })}
                            />
                          </td>
                          <td>
                            <input
                              type="text"
                              value={d.predecessors}
                              size={6}
                              aria-label={`Row ${d.row_index} predecessors`}
                              onChange={(e) => setDraft(d.row_index, { predecessors: e.target.value })}
                            />
                          </td>
                          <td style={{ whiteSpace: "nowrap" }}>
                            <label>
                              <input
                                type="checkbox"
                                checked={d.milestone}
                                aria-label={`Row ${d.row_index} milestone`}
                                onChange={(e) => setDraft(d.row_index, { milestone: e.target.checked })}
                              />
                              M
                            </label>{" "}
                            <label>
                              <input
                                type="checkbox"
                                checked={d.contractMilestone}
                                aria-label={`Row ${d.row_index} contract milestone`}
                                onChange={(e) =>
                                  setDraft(d.row_index, { contractMilestone: e.target.checked })
                                }
                              />
                              CM
                            </label>{" "}
                            <label>
                              <input
                                type="checkbox"
                                checked={d.delivery}
                                aria-label={`Row ${d.row_index} delivery`}
                                onChange={(e) => setDraft(d.row_index, { delivery: e.target.checked })}
                              />
                              D
                            </label>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </section>

            <section className="card dash-section" aria-label="Add a missing task">
              <h4>Add a task the document missed</h4>
              {added.map((a, i) => (
                <div className="dash-row" key={a.key}>
                  <input
                    type="text"
                    placeholder="Task name"
                    aria-label={`Added task ${i + 1} name`}
                    value={a.name}
                    onChange={(e) =>
                      setAdded((rows) => rows.map((x) => (x.key === a.key ? { ...x, name: e.target.value } : x)))
                    }
                  />
                  <input
                    type="text"
                    placeholder="Section"
                    size={12}
                    aria-label={`Added task ${i + 1} section`}
                    value={a.section}
                    onChange={(e) =>
                      setAdded((rows) => rows.map((x) => (x.key === a.key ? { ...x, section: e.target.value } : x)))
                    }
                  />
                  <input
                    type="text"
                    placeholder="Start (YYYY-MM-DD)"
                    size={12}
                    aria-label={`Added task ${i + 1} start date`}
                    value={a.start}
                    onChange={(e) =>
                      setAdded((rows) => rows.map((x) => (x.key === a.key ? { ...x, start: e.target.value } : x)))
                    }
                  />
                  <input
                    type="text"
                    placeholder="Finish"
                    size={12}
                    aria-label={`Added task ${i + 1} finish date`}
                    value={a.finish}
                    onChange={(e) =>
                      setAdded((rows) => rows.map((x) => (x.key === a.key ? { ...x, finish: e.target.value } : x)))
                    }
                  />
                  <input
                    type="text"
                    placeholder="%"
                    size={4}
                    aria-label={`Added task ${i + 1} percent`}
                    value={a.percent}
                    onChange={(e) =>
                      setAdded((rows) => rows.map((x) => (x.key === a.key ? { ...x, percent: e.target.value } : x)))
                    }
                  />
                  <button
                    type="button"
                    className="btn btn--secondary"
                    aria-label={`Remove added task ${i + 1}`}
                    onClick={() => {
                      setAdded((rows) => rows.filter((x) => x.key !== a.key));
                      setPlan(null);
                    }}
                  >
                    Remove
                  </button>
                </div>
              ))}
              <button
                type="button"
                className="btn btn--secondary"
                onClick={() => {
                  setAdded((rows) => [
                    ...rows,
                    { key: `added-${rows.length}-${Date.now()}`, name: "", section: "", duration: "", start: "", finish: "", percent: "" },
                  ]);
                  setPlan(null);
                }}
              >
                + Add a task
              </button>
            </section>

            <section className="card dash-section" aria-label="Preview and import">
              <h4>Import</h4>
              <button
                type="button"
                className="btn btn--secondary"
                disabled={busy !== null || !committable || resolvedRows.length === 0}
                onClick={() => void runPlan()}
              >
                {busy === "plan" ? "Checking…" : "Preview import"}
              </button>

              {plan ? (
                plan.degenerate ? (
                  <p aria-label="Preview result">
                    {plan.counts.incoming} task{plan.counts.incoming === 1 ? "" : "s"} → all new. This
                    becomes the job&apos;s task list.
                  </p>
                ) : (
                  <p className="dash-error" role="note">
                    This job already has a task list ({plan.counts.existing} task
                    {plan.counts.existing === 1 ? "" : "s"}), so this upload is a revision — go
                    back and open it again to review its changes on the reconcile screen. The
                    current list stays as it is.
                  </p>
                )
              ) : (
                <p className="dash__intro">
                  Preview first — it checks these tasks against the job without changing anything.
                </p>
              )}

              {progress ? <p role="status">Importing {progress}</p> : null}
              {!previewEvidence ? (
                <p className="dash__intro" role="note">
                  Import stays disabled until a source page has rendered here (or you confirm you
                  reviewed the PDF elsewhere) — the side-by-side check is the only thing that
                  catches an OCR misread.
                </p>
              ) : null}

              <div className="dash-row">
                <button
                  type="button"
                  className="btn btn--primary"
                  disabled={
                    busy !== null ||
                    !committable ||
                    plan === null ||
                    !plan.degenerate ||
                    problems.length > 0 ||
                    resolvedRows.length === 0 ||
                    !previewEvidence
                  }
                  onClick={() => void runCommit()}
                >
                  {busy === "commit" ? "Importing…" : `Import ${resolvedRows.length} tasks`}
                </button>
                <button
                  type="button"
                  className="btn btn--secondary"
                  disabled={busy !== null}
                  onClick={() => void runDiscard()}
                >
                  Discard this upload
                </button>
              </div>
              {schedule.status === "committing" ? (
                <p className="dash__intro" role="note">
                  This import stopped part-way. Importing again continues where it left off;
                  discarding stops here — tasks from pages that already imported stay on the list.
                </p>
              ) : null}
            </section>
          </div>
        </div>
      ) : null}
    </>
  );
}
