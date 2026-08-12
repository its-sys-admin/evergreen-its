import { useCallback, useEffect, useMemo, useState } from "react";
import * as api from "../lib/fieldops_schedules";
import type {
  ScheduleCommitRow,
  ScheduleListRow,
  SchedulePlanFresh,
  SchedulePlanRemoved,
  ScheduleResolutions,
} from "../lib/fieldops_schedules";
import { deriveGrid, draftProblem, toDurationDays, toPercent } from "../lib/schedule_grid";
import { errorText } from "../lib/errorCopy";

// ─────────────────────────────────────────────────────────────────────────────
// Schedule RECONCILE screen (ADR-0006 decision 9, PR-6) — the human half of a revision
// import onto a job with a LIVING task list.
//
// A SUB-FACE of JobSchedulePage (props + onClose, no PageShell — the ScheduleValidatePage
// pattern), mounted `key={scheduleId}` so switching schedules remounts rather than leaking
// the previous document's decisions into the next. JobSchedulePage routes here when the
// job already HAS tasks; the first-import validate face keeps the no-tasks path.
//
// PLAN-DRIVEN: the screen posts the derived rows to /plan and renders the server's
// classification in three columns —
//   • CHANGES  — matched pairs: old→new date chips, and the percent-CONFLICT radios
//                (portal/revision, default portal — decision 2: portal % is preserved
//                unless the human explicitly takes the revision's value);
//   • NEW      — fresh rows, each with a "link to removed task" picker (a RENAME: the
//                link preserves task_uuid, baselines and the portal %). The picker is a
//                client-side SUGGESTION ONLY — same section / equal dates merely SORT
//                the candidates; nothing is preselected (no fuzzy matching, decision 9);
//   • REMOVED  — living tasks the revision drops. BLOCKING ones (marked / delivered /
//                contract milestone) show their reason badge and REQUIRE an explicit
//                keep/remove; non-blocking ones default to remove, toggleable.
// Ambiguities (duplicate names) render as a blocking alert — there is no resolution
// channel for them; the human fixes the duplication itself and re-checks.
//
// The COMMIT re-derives the whole diff server-side and re-validates every resolution —
// this screen's classification is presentation, never authority. The commit is the same
// paged watermark loop as the first import, with the resolutions re-sent every round.
//
// The rows themselves are DERIVED, not edited, on this face (the shared
// src/lib/schedule_grid.ts derivation — identical to the validate face, so a task
// committed there matches its revision here). Rows the Worker would refuse (an OCR
// misread that broke a date/percent format) get a minimal fix-editor above the columns;
// everything else rides verbatim, with the source-page preview as the fidelity control.
// ─────────────────────────────────────────────────────────────────────────────

type RowEdit = Partial<{
  name: string; section: string; duration: string; start: string; finish: string; percent: string;
}>;

function errText(e: unknown, fallback: string): string {
  if (e && typeof e === "object" && "code" in e) {
    const code = (e as { code: string | null }).code;
    if (code) return errorText(code);
  }
  return e instanceof Error && e.message ? e.message : fallback;
}

/** Sort the removed-task candidates for one fresh row: same section first, then equal
 *  dates, then name — a SUGGESTED ORDER only, never a preselection (decision 9). */
function sortLinkCandidates(fresh: SchedulePlanFresh, removed: SchedulePlanRemoved[]): SchedulePlanRemoved[] {
  const score = (r: SchedulePlanRemoved): number => {
    let s = 0;
    if ((r.section ?? "") === (fresh.section ?? "")) s -= 2;
    if (r.start_date === fresh.start_date && r.finish_date === fresh.finish_date) s -= 1;
    return s;
  };
  return [...removed].sort((a, b) => score(a) - score(b) || a.name.localeCompare(b.name));
}

export function ScheduleReconcilePage({
  scheduleId,
  onClose,
}: {
  scheduleId: number;
  /** Leave the screen. `committed` tells the parent to reload its task list. */
  onClose: (notice?: { ok: boolean; text: string }, committed?: boolean) => void;
}) {
  const [schedule, setSchedule] = useState<
    (ScheduleListRow & { column_map_json: string | null; parse_notes: string | null }) | null
  >(null);
  const [previewPages, setPreviewPages] = useState<number[]>([]);
  const [notes, setNotes] = useState<string[]>([]);
  const [gridTasks, setGridTasks] = useState<ReturnType<typeof deriveGrid>["tasks"] | null>(null);
  const [edits, setEdits] = useState<Record<number, RowEdit>>({});
  const [loadError, setLoadError] = useState<string | null>(null);

  const [plan, setPlan] = useState<api.SchedulePlanResponse | null>(null);
  const [busy, setBusy] = useState<null | "plan" | "commit" | "discard">(null);
  const [progress, setProgress] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  // The human's decisions (the resolutions the commit sends).
  const [percentChoice, setPercentChoice] = useState<Record<number, "portal" | "revision">>({});
  const [linkChoice, setLinkChoice] = useState<Record<number, number | "">>({});
  const [removalChoice, setRemovalChoice] = useState<Record<number, "remove" | "keep">>({});

  // Preview pane (the validate face's reuse — same endpoints, same evidence gate, UX-only).
  const [page, setPage] = useState(1);
  const [previewSrc, setPreviewSrc] = useState<string | null>(null);
  const [previewLoaded, setPreviewLoaded] = useState(false);
  const [reviewedElsewhere, setReviewedElsewhere] = useState(false);

  const load = useCallback(() => {
    setLoadError(null);
    Promise.all([api.fetchSchedule(scheduleId), api.fetchAllScheduleRows(scheduleId)])
      .then(([d, rows]) => {
        const grid = deriveGrid(rows, d.schedule.column_map_json);
        setSchedule(d.schedule);
        setPreviewPages(d.preview_pages);
        setNotes((d.schedule.parse_notes ?? "").split("\n").filter(Boolean));
        setGridTasks(grid.tasks);
        if (d.preview_pages.length > 0) setPage(d.preview_pages[0]);
      })
      .catch((e) => setLoadError(errText(e, "Could not load this schedule.")));
  }, [scheduleId]);
  useEffect(load, [load]);

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

  /** The derived rows with the fix-editor's overrides applied — ONE derivation, shared
   *  by the plan, the problem list and the commit, so what was previewed IS what lands. */
  const derivedTasks = useMemo(() => {
    if (!gridTasks) return null;
    return [...gridTasks.values()]
      .map((t) => ({ ...t, ...(edits[t.row_index] ?? {}) }))
      .sort((a, b) => a.row_index - b.row_index);
  }, [gridTasks, edits]);

  const problems = useMemo(() => {
    if (!derivedTasks) return [];
    return derivedTasks
      .map((t) => ({ row_index: t.row_index, label: t.name.trim() || `row ${t.row_index}`, problem: draftProblem(t), task: t }))
      .filter((p): p is typeof p & { problem: string } => p.problem !== null);
  }, [derivedTasks]);

  const resolvedRows = useMemo((): ScheduleCommitRow[] => {
    if (!derivedTasks) return [];
    return derivedTasks.map((t) => ({
      source_row_index: t.row_index,
      kind: "data" as const,
      name: t.name.trim(),
      section: t.section.trim() || null,
      duration_days: toDurationDays(t.duration),
      start_date: t.start.trim() || null,
      finish_date: t.finish.trim() || null,
      percent_done: toPercent(t.percent) ?? 0,
      is_milestone: t.milestone,
      is_contract_milestone: false,
      is_delivery: t.delivery,
      predecessors_raw: t.predecessors.trim() || null,
    }));
  }, [derivedTasks]);

  const runPlan = useCallback(async () => {
    if (resolvedRows.length === 0) return;
    setBusy("plan");
    setMsg(null);
    try {
      const p = await api.planSchedule(scheduleId, resolvedRows);
      setPlan(p);
    } catch (e) {
      setMsg({ ok: false, text: errText(e, "Could not read the revision against the task list.") });
    } finally {
      setBusy(null);
    }
  }, [scheduleId, resolvedRows]);

  // Plan-driven: run the classification as soon as the rows exist (and only then).
  const [planned, setPlanned] = useState(false);
  useEffect(() => {
    if (planned || !derivedTasks || problems.length > 0 || resolvedRows.length === 0) return;
    setPlanned(true);
    void runPlan();
  }, [planned, derivedTasks, problems, resolvedRows, runPlan]);

  // ── Derived review sets ─────────────────────────────────────────────────────────────
  const linkedTargets = useMemo(
    () => new Set(Object.values(linkChoice).filter((v): v is number => typeof v === "number")),
    [linkChoice],
  );
  /** Removed tasks still removed once the chosen links are applied. */
  const effectiveRemoved = useMemo(
    () => (plan?.removed ?? []).filter((r) => !linkedTargets.has(r.task_id)),
    [plan, linkedTargets],
  );
  const conflicts = useMemo(
    () => (plan?.matched ?? []).filter((m) => m.percent.rule === "conflict"),
    [plan],
  );
  const unresolvedBlocking = useMemo(
    () => effectiveRemoved.filter((r) => r.blocking && removalChoice[r.task_id] === undefined),
    [effectiveRemoved, removalChoice],
  );

  const status = schedule?.status;
  const committable = status === "parsed" || status === "committing";
  const previewEvidence = previewLoaded || reviewedElsewhere;
  const commitReady =
    committable &&
    plan !== null &&
    !plan.degenerate &&
    plan.ambiguous.length === 0 &&
    problems.length === 0 &&
    unresolvedBlocking.length === 0 &&
    resolvedRows.length > 0 &&
    previewEvidence;

  function buildResolutions(): ScheduleResolutions {
    // Only links that still name a CURRENT fresh row and a CURRENT removed task — a
    // re-plan can reclassify rows out from under an earlier pick, and sending the
    // stale link would just earn the server's invalid_link_target 400.
    const freshIdx = new Set((plan?.fresh ?? []).map((f) => f.source_row_index));
    const removedIds = new Set((plan?.removed ?? []).map((r) => r.task_id));
    const links: Record<number, number> = {};
    for (const [idx, v] of Object.entries(linkChoice)) {
      if (typeof v === "number" && freshIdx.has(Number(idx)) && removedIds.has(v)) {
        links[Number(idx)] = v;
      }
    }
    const removals: Record<number, "remove" | "keep"> = {};
    for (const r of effectiveRemoved) {
      const choice = removalChoice[r.task_id] ?? (r.blocking ? undefined : "remove");
      if (choice) removals[r.task_id] = choice;
    }
    const percents: Record<number, "portal" | "revision"> = {};
    for (const m of conflicts) {
      percents[m.source_row_index] = percentChoice[m.source_row_index] ?? "portal";
    }
    return { links, removals, percents };
  }

  async function runCommit() {
    if (busy || !commitReady) return;
    setBusy("commit");
    setMsg(null);
    setProgress(null);
    try {
      const res = await api.commitAllSchedule(scheduleId, resolvedRows, {
        resolutions: buildResolutions(),
        onProgress: (landed, total) => setProgress(`${landed} of ${total} tasks…`),
      });
      onClose(
        {
          ok: true,
          text:
            `Revision applied — ${res.updated} updated, ${res.inserted} new, ` +
            `${res.linked} linked, ${res.removed} removed. This is now the job's governing schedule.`,
        },
        true,
      );
    } catch (e) {
      // HONEST failure copy: the commit is PAGED, so earlier pages may already have
      // updated the task list — never claim nothing landed.
      setMsg({
        ok: false,
        text: errText(
          e,
          "The revision stopped part-way. Changes from pages that already landed ARE on the task list — check the Schedule page, then run this again to continue where it left off.",
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
              ? "Revision discarded. Changes already applied by the interrupted run stay on the task list."
              : "Revision discarded — the task list is unchanged.",
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
  if (!schedule || !derivedTasks) {
    return (
      <section className="card dash-section" aria-label="Loading schedule">
        <p className="dash__intro">Loading the revision…</p>
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
        <span className="dash-pill dash-pill--warn">Revision reconcile</span>
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
            {schedule.status === "committed"
              ? "This upload is the job's governing schedule — it has already been applied."
              : schedule.status === "superseded"
                ? "A later revision replaced this upload — it is kept as revision history."
                : "This schedule can't be applied from its current state — refresh to see where it stands."}
          </p>
        </section>
      ) : null}

      {committable ? (
        <div
          className="schedule-reconcile__split"
          style={{
            display: "grid",
            gridTemplateColumns: "minmax(0, 1fr) minmax(0, 2fr)",
            gap: "1rem",
            alignItems: "start",
          }}
        >
          {/* ── LEFT: the source pages — the lane's fidelity control ────────────────── */}
          <section className="card dash-section" style={{ minWidth: 0 }} aria-label="Source document">
            <h4>Source</h4>
            {previewPages.length === 0 ? (
              <>
                <p className="dash__intro">
                  No page preview came back for this file — check the changes below against
                  your own copy of the PDF before applying.
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
                    style={{ maxWidth: "100%", border: "1px solid var(--c-border, #ccc)" }}
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

          {/* ── RIGHT: the plan-driven review ───────────────────────────────────────── */}
          <div style={{ minWidth: 0, display: "grid", gap: "1rem" }}>
            {problems.length > 0 ? (
              <section className="card dash-section" aria-label="Rows needing fixing">
                <div className="dash-error" role="alert">
                  {problems.length} row{problems.length === 1 ? " needs" : "s need"} fixing before
                  the revision can be checked — the reader's cell didn't parse:
                </div>
                {problems.slice(0, 20).map((p) => (
                  <div className="dash-row" key={p.row_index}>
                    <span>Row {p.row_index} — {p.problem}:</span>
                    <input
                      type="text"
                      value={p.task.name}
                      aria-label={`Fix row ${p.row_index} task name`}
                      onChange={(e) => {
                        setEdits((s) => ({ ...s, [p.row_index]: { ...s[p.row_index], name: e.target.value } }));
                        setPlan(null);
                        setPlanned(false);
                      }}
                    />
                    <input
                      type="text"
                      size={10}
                      value={p.task.start}
                      aria-label={`Fix row ${p.row_index} start date`}
                      onChange={(e) => {
                        setEdits((s) => ({ ...s, [p.row_index]: { ...s[p.row_index], start: e.target.value } }));
                        setPlan(null);
                        setPlanned(false);
                      }}
                    />
                    <input
                      type="text"
                      size={10}
                      value={p.task.finish}
                      aria-label={`Fix row ${p.row_index} finish date`}
                      onChange={(e) => {
                        setEdits((s) => ({ ...s, [p.row_index]: { ...s[p.row_index], finish: e.target.value } }));
                        setPlan(null);
                        setPlanned(false);
                      }}
                    />
                    <input
                      type="text"
                      size={4}
                      value={p.task.percent}
                      aria-label={`Fix row ${p.row_index} percent`}
                      onChange={(e) => {
                        setEdits((s) => ({ ...s, [p.row_index]: { ...s[p.row_index], percent: e.target.value } }));
                        setPlan(null);
                        setPlanned(false);
                      }}
                    />
                    <input
                      type="text"
                      size={5}
                      value={p.task.duration}
                      aria-label={`Fix row ${p.row_index} duration`}
                      onChange={(e) => {
                        setEdits((s) => ({ ...s, [p.row_index]: { ...s[p.row_index], duration: e.target.value } }));
                        setPlan(null);
                        setPlanned(false);
                      }}
                    />
                  </div>
                ))}
                {problems.length > 20 ? <p>…and {problems.length - 20} more</p> : null}
              </section>
            ) : null}

            {plan === null ? (
              <section className="card dash-section" aria-label="Checking the revision">
                <p className="dash__intro">
                  {problems.length > 0
                    ? "Fix the rows above, then check the revision."
                    : busy === "plan"
                      ? "Checking this revision against the task list…"
                      : "Check this revision against the task list."}
                </p>
                <button
                  type="button"
                  className="btn btn--secondary"
                  disabled={busy !== null || problems.length > 0 || resolvedRows.length === 0}
                  onClick={() => void runPlan()}
                >
                  {busy === "plan" ? "Checking…" : "Check revision"}
                </button>
              </section>
            ) : plan.degenerate ? (
              <section className="card dash-section" aria-label="No task list">
                <p className="dash-error" role="note">
                  This job&apos;s task list is empty now, so there is nothing to reconcile —
                  go back and open this upload again to import it as a first schedule.
                </p>
              </section>
            ) : (
              <>
                <p aria-label="Plan summary">
                  {plan.counts.matched} matched · {plan.counts.new} new · {plan.counts.removed}{" "}
                  removed
                  {plan.counts.ambiguous > 0 ? ` · ${plan.counts.ambiguous} ambiguous` : ""}
                  {plan.counts.percent_conflicts > 0
                    ? ` · ${plan.counts.percent_conflicts} percent conflict${plan.counts.percent_conflicts === 1 ? "" : "s"}`
                    : ""}
                </p>

                {plan.ambiguous.length > 0 ? (
                  <section className="card dash-section" aria-label="Ambiguous rows">
                    <div className="dash-error" role="alert">
                      <p>
                        {plan.ambiguous.length} row{plan.ambiguous.length === 1 ? "" : "s"} match
                        more than one task (duplicate names) — the revision can&apos;t apply until
                        the duplication is fixed. Rename or remove one of the duplicates on the
                        task list (or fix the row), then check again.
                      </p>
                      <ul aria-label="Ambiguous rows list">
                        {plan.ambiguous.slice(0, 20).map((a) => (
                          <li key={a.source_row_index}>
                            Row {a.source_row_index} ({a.name}) —{" "}
                            {a.reason === "duplicate_living"
                              ? `${a.candidates.length} tasks share this name`
                              : "several revision rows claim the same task"}
                          </li>
                        ))}
                      </ul>
                    </div>
                  </section>
                ) : null}

                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "repeat(auto-fit, minmax(16rem, 1fr))",
                    gap: "1rem",
                    alignItems: "start",
                  }}
                >
                  {/* ── CHANGES ─────────────────────────────────────────────────────── */}
                  <section className="card dash-section" style={{ minWidth: 0 }} aria-label="Changes">
                    <h4>Changes ({plan.matched.length})</h4>
                    {plan.matched.length === 0 ? (
                      <p className="dash__intro">No existing task is touched.</p>
                    ) : null}
                    <ul aria-label="Matched tasks">
                      {plan.matched.map((m) => (
                        <li key={m.source_row_index}>
                          <strong>{m.name}</strong>
                          {m.date_change ? (
                            <div>
                              <span className="dash-pill">
                                {m.date_change.start_from ?? "—"} → {m.date_change.start_to ?? "—"}
                              </span>{" "}
                              <span className="dash-pill">
                                {m.date_change.finish_from ?? "—"} → {m.date_change.finish_to ?? "—"}
                              </span>
                            </div>
                          ) : null}
                          {m.percent.rule === "conflict" ? (
                            <div role="radiogroup" aria-label={`Percent conflict for ${m.name}`}>
                              <label style={{ whiteSpace: "nowrap" }}>
                                <input
                                  type="radio"
                                  name={`pct-${m.source_row_index}`}
                                  checked={(percentChoice[m.source_row_index] ?? "portal") === "portal"}
                                  aria-label={`Keep portal ${m.percent.portal}% for ${m.name}`}
                                  onChange={() =>
                                    setPercentChoice((s) => ({ ...s, [m.source_row_index]: "portal" }))
                                  }
                                />{" "}
                                Keep field mark ({m.percent.portal}%)
                              </label>{" "}
                              <label style={{ whiteSpace: "nowrap" }}>
                                <input
                                  type="radio"
                                  name={`pct-${m.source_row_index}`}
                                  checked={percentChoice[m.source_row_index] === "revision"}
                                  aria-label={`Take revision ${m.percent.revision}% for ${m.name}`}
                                  onChange={() =>
                                    setPercentChoice((s) => ({ ...s, [m.source_row_index]: "revision" }))
                                  }
                                />{" "}
                                Take revision ({m.percent.revision}%)
                              </label>
                            </div>
                          ) : m.percent.rule === "take_revision" &&
                              m.percent.portal !== m.percent.revision ? (
                            <div className="dash__intro">
                              % {m.percent.portal} → {m.percent.revision}
                            </div>
                          ) : null}
                          {m.info_changes.length > 0 ? (
                            <div className="dash__intro">also: {m.info_changes.join(", ")}</div>
                          ) : null}
                        </li>
                      ))}
                    </ul>
                  </section>

                  {/* ── NEW ─────────────────────────────────────────────────────────── */}
                  <section className="card dash-section" style={{ minWidth: 0 }} aria-label="New tasks">
                    <h4>New ({plan.fresh.length})</h4>
                    {plan.fresh.length === 0 ? (
                      <p className="dash__intro">The revision adds no tasks.</p>
                    ) : null}
                    <ul aria-label="New tasks list">
                      {plan.fresh.map((f) => (
                        <li key={f.source_row_index}>
                          <strong>{f.name}</strong>
                          {f.section ? <span className="dash__intro"> · {f.section}</span> : null}
                          {plan.removed.length > 0 ? (
                            <div>
                              <select
                                aria-label={`Link ${f.name} to a removed task`}
                                value={linkChoice[f.source_row_index] ?? ""}
                                onChange={(e) =>
                                  setLinkChoice((s) => ({
                                    ...s,
                                    [f.source_row_index]:
                                      e.target.value === "" ? "" : Number(e.target.value),
                                  }))
                                }
                              >
                                <option value="">New task (not a rename)</option>
                                {sortLinkCandidates(f, plan.removed)
                                  // A removed task may be linked by ONE fresh row only.
                                  .filter(
                                    (r) =>
                                      !linkedTargets.has(r.task_id) ||
                                      linkChoice[f.source_row_index] === r.task_id,
                                  )
                                  .map((r) => (
                                    <option key={r.task_id} value={r.task_id}>
                                      Same task as: {r.name}
                                      {r.section ? ` (${r.section})` : ""}
                                      {r.percent_done ? ` — ${r.percent_done}%` : ""}
                                    </option>
                                  ))}
                              </select>
                            </div>
                          ) : null}
                        </li>
                      ))}
                    </ul>
                    {plan.removed.length > 0 && plan.fresh.length > 0 ? (
                      <p className="dash__intro">
                        A rename shows up as one new + one removed task — linking them keeps the
                        task&apos;s history, baselines and field progress.
                      </p>
                    ) : null}
                  </section>

                  {/* ── REMOVED ─────────────────────────────────────────────────────── */}
                  <section className="card dash-section" style={{ minWidth: 0 }} aria-label="Removed tasks">
                    <h4>Removed ({effectiveRemoved.length})</h4>
                    {effectiveRemoved.length === 0 ? (
                      <p className="dash__intro">The revision drops no tasks.</p>
                    ) : null}
                    <ul aria-label="Removed tasks list">
                      {effectiveRemoved.map((r) => (
                        <li key={r.task_id}>
                          <strong>{r.name}</strong>
                          {r.blocking
                            ? r.reasons.map((reason) => (
                                <span key={reason} className="dash-pill dash-pill--warn">
                                  {reason === "marked"
                                    ? `field-marked ${r.percent_done}%`
                                    : reason === "delivered"
                                      ? "delivered"
                                      : "contract milestone"}
                                </span>
                              ))
                            : null}
                          <div role="radiogroup" aria-label={`Removal decision for ${r.name}`}>
                            <label style={{ whiteSpace: "nowrap" }}>
                              <input
                                type="radio"
                                name={`rm-${r.task_id}`}
                                checked={
                                  (removalChoice[r.task_id] ?? (r.blocking ? undefined : "remove")) ===
                                  "remove"
                                }
                                aria-label={`Remove ${r.name}`}
                                onChange={() =>
                                  setRemovalChoice((s) => ({ ...s, [r.task_id]: "remove" }))
                                }
                              />{" "}
                              Remove
                            </label>{" "}
                            <label style={{ whiteSpace: "nowrap" }}>
                              <input
                                type="radio"
                                name={`rm-${r.task_id}`}
                                checked={removalChoice[r.task_id] === "keep"}
                                aria-label={`Keep ${r.name}`}
                                onChange={() =>
                                  setRemovalChoice((s) => ({ ...s, [r.task_id]: "keep" }))
                                }
                              />{" "}
                              Keep on the list
                            </label>
                          </div>
                        </li>
                      ))}
                    </ul>
                    {unresolvedBlocking.length > 0 ? (
                      <p className="dash-error" role="note">
                        {unresolvedBlocking.length} removed task
                        {unresolvedBlocking.length === 1 ? " has" : "s have"} field progress, a
                        delivered mark, or a contract-milestone flag — choose keep or remove for
                        each before applying.
                      </p>
                    ) : null}
                  </section>
                </div>

                {/* ── Apply ─────────────────────────────────────────────────────────── */}
                <section className="card dash-section" aria-label="Apply the revision">
                  {progress ? <p role="status">Applying {progress}</p> : null}
                  {!previewEvidence ? (
                    <p className="dash__intro" role="note">
                      Apply stays disabled until a source page has rendered here (or you confirm
                      you reviewed the PDF elsewhere) — the side-by-side check is the only thing
                      that catches an OCR misread.
                    </p>
                  ) : null}
                  <div className="dash-row">
                    <button
                      type="button"
                      className="btn btn--primary"
                      disabled={busy !== null || !commitReady}
                      onClick={() => void runCommit()}
                    >
                      {busy === "commit" ? "Applying…" : "Apply revision"}
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
                      This revision stopped part-way. Applying again continues where it left off;
                      discarding stops here — changes that already landed stay on the list.
                    </p>
                  ) : null}
                </section>
              </>
            )}
          </div>
        </div>
      ) : null}
    </>
  );
}
