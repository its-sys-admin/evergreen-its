import { useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import * as api from "./fieldops_schedules";
import type { ScheduleTaskRow } from "./fieldops_schedules";
import { errorText } from "./errorCopy";

// Shared mark-off logic for schedule tasks (extracted from JobSchedulePage, Track A4 2026-08-13,
// §14 parameterize-not-clone): the optimistic-patch → API call → reload-or-honest-revert loop,
// the quick-%/exact-% pair, the binary milestone rule (un-checking is 0%), and the delivered-date
// default. TWO surfaces render these marks — the Job Schedule page and the Site Tasks page — and
// none of those semantics may drift between them, so they live once, here.

export type MarkMsg = { ok: boolean; text: string } | null;

function errText(e: unknown, fallback: string): string {
  if (e && typeof e === "object" && "code" in e) {
    const code = (e as { code: string | null }).code;
    if (code) return errorText(code);
  }
  return fallback;
}

export function useScheduleMarks(opts: {
  setTasks: Dispatch<SetStateAction<ScheduleTaskRow[] | null>>;
  /** Re-fetch the task list — confirms the server's row on success and honestly reverts the
   *  optimistic patch on failure. */
  reload: () => void;
  setMsg: (m: MarkMsg) => void;
  today: string;
}) {
  const { setTasks, reload, setMsg, today } = opts;
  const [markBusy, setMarkBusy] = useState<number | null>(null);
  const [exactPct, setExactPct] = useState<Record<number, string>>({});
  const [deliveredDraft, setDeliveredDraft] = useState<Record<number, string>>({});

  /** Optimistically patch one task row, run the mark call, then reload — the reload
   *  confirms the server's row on success and honestly reverts the optimism on failure. */
  async function runMark(
    taskId: number,
    patch: Partial<ScheduleTaskRow>,
    callFn: () => Promise<unknown>,
    failText: string,
  ) {
    if (markBusy !== null) return;
    setMarkBusy(taskId);
    setMsg(null);
    setTasks((prev) => (prev ? prev.map((t) => (t.id === taskId ? { ...t, ...patch } : t)) : prev));
    try {
      await callFn();
    } catch (e) {
      setMsg({ ok: false, text: errText(e, failText) });
    } finally {
      setMarkBusy(null);
      reload();
    }
  }

  function markPercent(t: ScheduleTaskRow, percent: number) {
    return runMark(
      t.id,
      { percent_done: percent },
      () => api.markScheduleTaskProgress(t.id, percent),
      "Could not save that progress mark.",
    );
  }

  function markExact(t: ScheduleTaskRow) {
    const raw = (exactPct[t.id] ?? "").trim();
    const val = Number(raw);
    // Mirror the Worker's bound locally so a typo fails instantly, not after a round trip.
    if (!raw.length || !Number.isInteger(val) || val < 0 || val > 100) {
      setMsg({ ok: false, text: "Progress must be a whole number from 0 to 100." });
      return;
    }
    setExactPct((prev) => ({ ...prev, [t.id]: "" }));
    void markPercent(t, val);
  }

  function markMilestone(t: ScheduleTaskRow, done: boolean) {
    if (done) {
      return runMark(
        t.id,
        { percent_done: 100 },
        () => api.markScheduleTaskMilestoneDone(t.id),
        "Could not save that done mark.",
      );
    }
    // Un-checking is a correction — a milestone is binary, so "not done" is 0%.
    return markPercent(t, 0);
  }

  function markDelivered(t: ScheduleTaskRow) {
    const date = deliveredDraft[t.id] ?? t.delivered_date ?? today;
    return runMark(
      t.id,
      { delivered_date: date },
      () => api.markScheduleTaskDelivered(t.id, date),
      "Could not save the delivered mark.",
    );
  }

  return {
    markBusy, exactPct, setExactPct, deliveredDraft, setDeliveredDraft,
    markPercent, markExact, markMilestone, markDelivered,
  };
}
