// Shared derivation: OCR-proposed grid rows → task-row drafts (ADR-0006).
//
// Extracted from ScheduleValidatePage when the PR-6 reconcile face landed, because the
// two sub-faces MUST resolve a grid row to (name, section, dates, …) identically: the
// reconcile diff matches on scheduleMatchKey(section, name), so if the validate face and
// the reconcile face disagreed about which cell is the name or which running section a
// row sits under, a task committed by one face would silently never match its revision
// read by the other — the exact drift class the shared Worker-side normalizer kills.
//
// Everything here is a PROPOSAL seed (§4 no-invented-field-data): the validate face lets
// the human edit every cell; the reconcile face shows the derived rows against the living
// list and the source-page preview. Nothing auto-corrects.

import type { ScheduleColumnMap, ScheduleGridRow } from "../../worker/wire-types";

/** CANONICAL_COLUMNS order (field_ops/schedule_parse.py) — the fallback indices when the
 *  detail's column_map is absent/malformed. The column_map in the detail response confirms
 *  the real indices; the parser currently emits exactly this order. */
export const DEFAULT_MAPPING: Record<string, number> = {
  row_number: 0, task_name: 1, duration: 2, start_date: 3,
  finish_date: 4, percent_done: 5, predecessors: 6, phase: 7,
};

export function parseCells(raw: string): string[] {
  try {
    const v = JSON.parse(raw) as unknown;
    return Array.isArray(v) ? v.map((c) => String(c ?? "")) : [];
  } catch {
    return [];
  }
}

export function parseColumnMap(raw: string | null): ScheduleColumnMap | null {
  if (!raw) return null;
  try {
    return JSON.parse(raw) as ScheduleColumnMap;
  } catch {
    return null;
  }
}

/** "137d" / "137" → 137; anything else null. */
export function toDurationDays(text: string): number | null {
  const m = /^(\d{1,4})d?$/.exec(text.trim());
  if (!m) return null;
  return parseInt(m[1], 10);
}

export function toPercent(text: string): number | null {
  const m = /^(\d{1,3})%?$/.exec(text.trim());
  if (!m) return null;
  const n = parseInt(m[1], 10);
  return n >= 0 && n <= 100 ? n : null;
}

/** One data row's derived task fields, still as the grid's raw strings (the validate
 *  face edits them; the reconcile face converts them straight to commit rows). */
export interface DerivedGridTask {
  row_index: number;
  name: string;
  section: string;
  duration: string; // tolerates the export's "137d" spelling
  start: string;
  finish: string;
  percent: string;
  predecessors: string;
  milestone: boolean;
  delivery: boolean;
  flags: string[];
}

/** A non-task display row (section divider / header / meta), kept for rendering. */
export type DerivedGridItem =
  | { type: "section"; row_index: number; label: string }
  | { type: "task"; row_index: number }
  | { type: "other"; row_index: number; kind: string; text: string };

export interface DerivedGrid {
  items: DerivedGridItem[];
  tasks: Map<number, DerivedGridTask>;
}

/**
 * Resolve the proposed grid into task drafts + display order. Section rows feed the
 * RUNNING section context of the data rows below them; a data row whose own phase cell
 * is non-empty keeps it (the parser already resolved grid-view phase tags there).
 * Milestone/delivery pre-checks mirror the parser's own proposal heuristics
 * (schedule_parse.py): a same-day or zero/one-day task proposes milestone; a
 * Deliveries-phase task (or one literally named "… delivery") proposes delivery.
 * Contract-milestone has no OCR signal — always a human decision, never derived here.
 */
export function deriveGrid(rows: ScheduleGridRow[], columnMapJson: string | null): DerivedGrid {
  const cmap = parseColumnMap(columnMapJson);
  const mapping = { ...DEFAULT_MAPPING, ...(cmap?.mapping ?? {}) };
  const col = (r: ScheduleGridRow, concept: string): string => {
    const idx = mapping[concept];
    if (idx === undefined) return "";
    return parseCells(r.cells_json)[idx] ?? "";
  };
  const tasks = new Map<number, DerivedGridTask>();
  const items: DerivedGridItem[] = [];
  let runningSection = "";
  for (const r of rows) {
    if (r.kind === "section") {
      const label = col(r, "task_name") || parseCells(r.cells_json).find((c) => c.trim()) || "";
      runningSection = label;
      items.push({ type: "section", row_index: r.row_index, label });
      continue;
    }
    if (r.kind !== "data" && r.kind !== "continuation") {
      const text = parseCells(r.cells_json).filter((c) => c.trim()).join(" · ");
      items.push({ type: "other", row_index: r.row_index, kind: r.kind, text });
      continue;
    }
    const name = col(r, "task_name");
    const section = col(r, "phase") || runningSection;
    const start = col(r, "start_date");
    const finish = col(r, "finish_date");
    const duration = col(r, "duration");
    const milestone =
      (!!start && !!finish && start === finish) || duration === "0d" || duration === "1d";
    const delivery =
      section.trim().toLowerCase() === "deliveries" ||
      name.trim().toLowerCase().endsWith("delivery");
    tasks.set(r.row_index, {
      row_index: r.row_index,
      name,
      section,
      duration,
      start,
      finish,
      percent: col(r, "percent_done"),
      predecessors: col(r, "predecessors"),
      milestone,
      delivery,
      flags: (r.flags ?? "").split(",").filter(Boolean),
    });
    items.push({ type: "task", row_index: r.row_index });
  }
  return { items, tasks };
}

/** The blocking problem on a draft, or null when it would commit cleanly. Mirrors the
 *  Worker's readScheduleTaskFields bounds so one bad OCR cell blocks with a named row
 *  instead of a whole-commit Worker 400. */
export function draftProblem(d: {
  name: string; section: string; duration: string; start: string; finish: string;
  percent: string; predecessors?: string;
}): string | null {
  const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
  if (!d.name.trim()) return "no task name";
  if (d.name.trim().length > 300) return "name too long (300 max)";
  if (d.section.trim().length > 120) return "section too long (120 max)";
  if (d.start.trim() && !DATE_RE.test(d.start.trim())) return "start date isn't YYYY-MM-DD";
  if (d.finish.trim() && !DATE_RE.test(d.finish.trim())) return "finish date isn't YYYY-MM-DD";
  if (d.percent.trim() && toPercent(d.percent) === null) return "% must be a whole number 0–100";
  if (d.duration.trim() && (toDurationDays(d.duration) === null || toDurationDays(d.duration)! > 5000)) {
    return "duration must be a number of days (0–5000)";
  }
  if ((d.predecessors ?? "").trim().length > 200) return "predecessors too long (200 max)";
  return null;
}
