import { describe, it, expect } from "vitest";
import {
  diffSchedule,
  percentPlan,
  removalReasons,
  type DiffLivingTask,
  type DiffProposedRow,
  type DiffTaskFields,
} from "../worker/schedule_diff";
import { scheduleMatchKey } from "../worker/schedule_normalize";

// ─────────────────────────────────────────────────────────────────────────────
// schedule_diff — the PURE reconcile classifier (ADR-0006 decision 9, PR-6).
//
// Table-driven on purpose: the module is a deterministic function, so the whole
// decision table lives here; the worker suite (fieldops-schedule-tasks.test.ts)
// covers only what plain vitest structurally cannot — the routes, the watermark,
// D1 state. NO fuzzy matching is the load-bearing property: a rename MUST come out
// as fresh+removed, never a similarity guess.
// ─────────────────────────────────────────────────────────────────────────────

let nextId = 1;
function task(name: string, over: Partial<DiffLivingTask> = {}): DiffLivingTask {
  const id = over.id ?? nextId++;
  const section = over.section === undefined ? "Civil" : over.section;
  return {
    id,
    task_uuid: `tu-${id}`,
    match_key: scheduleMatchKey(section ?? "", name),
    section,
    name,
    duration_days: 5,
    start_date: "2026-09-01",
    finish_date: "2026-09-05",
    percent_done: 25,
    schedule_percent: 25,
    last_marked_by: null,
    delivered_date: null,
    is_milestone: 0,
    is_contract_milestone: 0,
    is_delivery: 0,
    predecessors_raw: null,
    sort_order: id * 10,
    ...over,
  };
}

function fields(name: string, over: Partial<DiffTaskFields> = {}): DiffTaskFields {
  return {
    name,
    section: over.section === undefined ? "Civil" : over.section,
    duration_days: 5,
    start_date: "2026-09-01",
    finish_date: "2026-09-05",
    percent_done: 25,
    is_milestone: 0,
    is_contract_milestone: 0,
    is_delivery: 0,
    predecessors_raw: null,
    ...over,
  };
}

function row(idx: number, name: string, over: Partial<DiffTaskFields> = {}): DiffProposedRow {
  return { source_row_index: idx, fields: fields(name, over) };
}

describe("diffSchedule — classification table", () => {
  it("identical revision → everything matched, nothing fresh/removed/ambiguous, no changes", () => {
    const living = [task("Fencing"), task("Grading")];
    const d = diffSchedule({ proposed: [row(1, "Fencing"), row(2, "Grading")], living });
    expect(d.matched.map((m) => [m.source_row_index, m.task.name])).toEqual([
      [1, "Fencing"], [2, "Grading"],
    ]);
    expect(d.fresh).toEqual([]);
    expect(d.removed).toEqual([]);
    expect(d.ambiguous).toEqual([]);
    expect(d.matched[0].date_change).toBeNull();
    expect(d.matched[0].percent).toEqual({ rule: "keep_portal", portal: 25, revision: 25 });
    expect(d.matched[0].info_changes).toEqual([]);
  });

  it("matching is the NORMALIZED key: case / whitespace / edge-punctuation variants still match, and surface as a silent 'name' info change", () => {
    const living = [task("Fence Install")];
    const d = diffSchedule({ proposed: [row(1, "  FENCE   install |", { section: "CIVIL" })], living });
    expect(d.matched).toHaveLength(1);
    expect(d.fresh).toEqual([]);
    expect(d.removed).toEqual([]);
    expect(d.matched[0].info_changes).toContain("name");
    expect(d.matched[0].info_changes).toContain("section");
  });

  it("a DATE SLIP is matched with the two-way date_change (from = living, to = revision)", () => {
    const living = [task("Fencing")];
    const d = diffSchedule({
      proposed: [row(1, "Fencing", { start_date: "2026-09-03", finish_date: "2026-09-09" })],
      living,
    });
    expect(d.matched[0].date_change).toEqual({
      start_from: "2026-09-01", start_to: "2026-09-03",
      finish_from: "2026-09-05", finish_to: "2026-09-09",
    });
  });

  it("an ADD is fresh; a DELETE of an untouched task is a NON-blocking removal (default remove)", () => {
    const living = [task("Fencing"), task("Old Task")];
    const d = diffSchedule({ proposed: [row(1, "Fencing"), row(2, "Brand New")], living });
    expect(d.fresh.map((f) => f.fields.name)).toEqual(["Brand New"]);
    expect(d.removed).toHaveLength(1);
    expect(d.removed[0].task.name).toBe("Old Task");
    expect(d.removed[0].blocking).toBe(false);
    expect(d.removed[0].reasons).toEqual([]);
  });

  it("a RENAME surfaces as fresh + removed — never a fuzzy match (decision 9)", () => {
    const living = [task("Fencing Phase 1")];
    const d = diffSchedule({ proposed: [row(1, "Fencing Phase One")], living });
    expect(d.matched).toEqual([]);
    expect(d.fresh.map((f) => f.fields.name)).toEqual(["Fencing Phase One"]);
    expect(d.removed.map((r) => r.task.name)).toEqual(["Fencing Phase 1"]);
  });

  it("a DELIVERED task's date change is an ordinary matched pair (the delivered mark is not a diffed field) — but DROPPING it blocks", () => {
    const delivered = task("Pile Delivery", {
      section: "Deliveries", is_delivery: 1, delivered_date: "2026-09-04",
    });
    const moved = diffSchedule({
      proposed: [row(1, "Pile Delivery", {
        section: "Deliveries", is_delivery: 1,
        start_date: "2026-09-08", finish_date: "2026-09-08",
      })],
      living: [delivered],
    });
    expect(moved.matched).toHaveLength(1);
    expect(moved.matched[0].date_change).not.toBeNull();
    const dropped = diffSchedule({ proposed: [row(1, "Something else")], living: [delivered] });
    expect(dropped.removed).toHaveLength(1);
    expect(dropped.removed[0].blocking).toBe(true);
    expect(dropped.removed[0].reasons).toEqual(["delivered"]);
  });
});

describe("diffSchedule — ambiguity (BLOCKING, both directions)", () => {
  it(">1 living tasks share the key → duplicate_living, candidates named, tasks NEVER classified removed (limbo)", () => {
    const a = task("Fencing", { id: 101, sort_order: 10 });
    const b = task("Fencing", { id: 102, sort_order: 20 });
    const d = diffSchedule({ proposed: [row(1, "Fencing")], living: [a, b] });
    expect(d.ambiguous).toEqual([
      {
        source_row_index: 1, name: "Fencing", section: "Civil",
        candidates: [101, 102], reason: "duplicate_living",
      },
    ]);
    expect(d.matched).toEqual([]);
    // The duplicate tasks are owned by the ambiguity — offering them as removals
    // would invite a destructive resolution while the commit is blocked anyway.
    expect(d.removed).toEqual([]);
  });

  it("≥2 proposed rows hit the SAME living task → duplicate_proposed for each claimant; the task is in limbo", () => {
    const t = task("Fencing", { id: 201 });
    const d = diffSchedule({ proposed: [row(1, "Fencing"), row(2, "fencing")], living: [t] });
    expect(d.ambiguous.map((a) => [a.source_row_index, a.reason, a.candidates])).toEqual([
      [1, "duplicate_proposed", [201]],
      [2, "duplicate_proposed", [201]],
    ]);
    expect(d.matched).toEqual([]);
    expect(d.removed).toEqual([]);
    expect(d.fresh).toEqual([]);
  });

  it("duplicate names ONLY in the document with no living hit are two fresh rows, not ambiguity (they claim nothing)", () => {
    const d = diffSchedule({ proposed: [row(1, "Fencing 2"), row(2, "Fencing 2")], living: [] });
    expect(d.ambiguous).toEqual([]);
    expect(d.fresh).toHaveLength(2);
  });
});

describe("percentPlan — the three-way rule, all arms", () => {
  it("rev == schedule_percent → keep_portal (even when the portal moved: a field mark stands)", () => {
    const t = task("Fencing", { percent_done: 75, schedule_percent: 25, last_marked_by: "sub.sam" });
    expect(percentPlan(fields("Fencing", { percent_done: 25 }), t)).toEqual({
      rule: "keep_portal", portal: 75, revision: 25,
    });
  });

  it("rev changed + last_marked_by NULL → take_revision (nobody field-marked; the schedule is the only voice)", () => {
    const t = task("Fencing", { percent_done: 25, schedule_percent: 25, last_marked_by: null });
    expect(percentPlan(fields("Fencing", { percent_done: 60 }), t)).toEqual({
      rule: "take_revision", portal: 25, revision: 60,
    });
  });

  it("rev changed + field-marked → CONFLICT carrying both numbers (default keep portal at commit)", () => {
    const t = task("Fencing", { percent_done: 50, schedule_percent: 25, last_marked_by: "sub.sam" });
    expect(percentPlan(fields("Fencing", { percent_done: 60 }), t)).toEqual({
      rule: "conflict", portal: 50, revision: 60,
    });
  });

  it("schedule_percent NULL (a hand-added task) → the rev==schedule arm can't fire: unmarked takes rev, marked conflicts", () => {
    const unmarked = task("Manual", { percent_done: 0, schedule_percent: null, last_marked_by: null });
    expect(percentPlan(fields("Manual", { percent_done: 30 }), unmarked).rule).toBe("take_revision");
    const marked = task("Manual", { percent_done: 40, schedule_percent: null, last_marked_by: "sub.sam" });
    expect(percentPlan(fields("Manual", { percent_done: 30 }), marked).rule).toBe("conflict");
  });
});

describe("removalReasons — the blocking classes (never silently destroyed)", () => {
  it("marked / delivered / contract-milestone each block; they compound; a clean task doesn't", () => {
    expect(removalReasons(task("A", { last_marked_by: "sub.sam" }))).toEqual(["marked"]);
    expect(removalReasons(task("B", { delivered_date: "2026-09-04" }))).toEqual(["delivered"]);
    expect(removalReasons(task("C", { is_contract_milestone: 1 }))).toEqual(["contract_milestone"]);
    expect(
      removalReasons(
        task("D", { last_marked_by: "x", delivered_date: "2026-09-04", is_contract_milestone: 1 }),
      ),
    ).toEqual(["marked", "delivered", "contract_milestone"]);
    expect(removalReasons(task("E"))).toEqual([]);
  });

  it("…and diffSchedule carries them onto the removed entries", () => {
    const living = [
      task("Marked", { last_marked_by: "sub.sam" }),
      task("Plain"),
      task("Contract SC", { is_contract_milestone: 1 }),
    ];
    const d = diffSchedule({ proposed: [row(1, "Unrelated")], living });
    expect(d.removed.map((r) => [r.task.name, r.blocking, r.reasons])).toEqual([
      ["Marked", true, ["marked"]],
      ["Plain", false, []],
      ["Contract SC", true, ["contract_milestone"]],
    ]);
  });
});

describe("diffSchedule — field diff details + determinism", () => {
  it("duration / predecessors / milestone-delivery flags tag info_changes; contract-milestone deliberately does NOT (it is preserved, not applied)", () => {
    const living = [task("Fencing", { is_contract_milestone: 1 })];
    const d = diffSchedule({
      proposed: [row(1, "Fencing", {
        duration_days: 9, predecessors_raw: "4FS", is_milestone: 1,
        is_contract_milestone: 0, // the OCR never proposes CM — must not flag a change
      })],
      living,
    });
    expect(d.matched[0].info_changes).toEqual(["duration", "predecessors", "flags"]);
  });

  it("outputs are deterministically ordered: matched/ambiguous/fresh by source_row_index, removed by living document order", () => {
    const living = [
      task("Z last", { id: 5, sort_order: 50 }),
      task("A first", { id: 3, sort_order: 10 }),
      task("Kept", { id: 4, sort_order: 30 }),
    ];
    const d1 = diffSchedule({
      proposed: [row(9, "New Nine"), row(2, "Kept"), row(7, "New Seven")],
      living,
    });
    const d2 = diffSchedule({
      proposed: [row(7, "New Seven"), row(9, "New Nine"), row(2, "Kept")],
      living: [...living].reverse(),
    });
    for (const d of [d1, d2]) {
      expect(d.fresh.map((f) => f.source_row_index)).toEqual([7, 9]);
      expect(d.matched.map((m) => m.source_row_index)).toEqual([2]);
      expect(d.removed.map((r) => r.task.id)).toEqual([3, 5]); // sort_order 10 then 50
    }
  });

  it("a section move changes the match key — the task shows as fresh+removed, not matched (sections are identity)", () => {
    const living = [task("Trenching", { section: "Civil" })];
    const d = diffSchedule({ proposed: [row(1, "Trenching", { section: "Electrical" })], living });
    expect(d.matched).toEqual([]);
    expect(d.fresh).toHaveLength(1);
    expect(d.removed).toHaveLength(1);
  });
});
