-- Job-schedule LIVING TASK LIST (ADR-0006, PR-4) — the per-job task list a committed
-- schedule import authors and the portal marks off. Where migration 0066 built the
-- IMPORT pool (the untrusted document, its OCR-proposed grid, its previews), this table
-- holds what survives the human validate screen's commit: one row per task the job is
-- actually tracked against. The pool is evidence; THIS is the record.
--
-- WHY A SEPARATE TABLE: the grid rows (job_schedule_rows) are the parser's verbatim
-- PROPOSAL, retained after commit as the next revision's reconcile evidence — they must
-- never be edited by marks or reconciles. The task list is the LIVING projection: it
-- accumulates portal progress marks (PR-5), survives schedule revisions (PR-6's
-- three-way diff re-keys onto it), and is what the Schedule page renders.
--
-- IDENTITY: `task_uuid` is the task's STABLE identity across revisions — a PR-6
-- reconcile that matches a revision row onto an existing task PRESERVES the uuid (and
-- with it the baselines and the portal %). `match_key` is the Worker-computed matching
-- key (worker/schedule_normalize.ts scheduleMatchKey: normalize(section)+'\n'+
-- normalize(name)) — deliberately NOT unique: duplicate task names inside a section are
-- real in the corpus and become BLOCKING-ambiguous reconcile outcomes for the human,
-- never a silent first-match (ADR-0006 decision 8/9).
--
-- BASELINE IMMUTABILITY: baseline_start_date / baseline_finish_date are stamped ONCE,
-- at the task's OWN first commit, and never rewritten — they are the slip-measurement
-- anchor. A later revision that moves the dates updates start_date/finish_date only;
-- the gap between baseline and current IS the slip a future alert engine reads.
--
-- THE THREE PERCENTS (decision 9's diff base):
--   • percent_done       — what the PORTAL currently believes (marks move it; PR-4's
--                          degenerate commit seeds it from the schedule).
--   • schedule_percent   — what the LAST COMMITTED schedule asserted. The PR-6 three-way
--                          diff compares a revision's % against THIS, not against
--                          percent_done, so a portal mark since the last import is
--                          distinguishable from a PM's re-export edit.
--   • last_marked_by/at  — stamped ONLY by portal marks (PR-5). Commits leave them
--                          NULL, and office edits through the task-edit route leave
--                          them alone too: `last_marked_by IS NOT NULL` ⇔ "a human
--                          marked this in the portal", which is the %-conflict
--                          predicate the reconcile rule keys on.
--
-- DELIVERY (decision 5): is_delivery marks Deliveries-phase tasks; delivered_date/
-- delivered_by/delivered_at are the explicit delivered mark (PR-5 route). delivered_by
-- stores the account username; every read resolves it to the personnel display name
-- (W9) and never serves the raw username.
--
-- predecessors_raw is VERBATIM, never parsed (the corpus only sometimes carries it) —
-- display + provenance only, no dependency engine.
--
-- SOFT DELETE: active=0 (the job_expected_materials posture) — a task with marks is
-- history, not something a DELETE should be able to erase. purge-job is the hard exit.
--
-- APPLY BEFORE DEPLOY: run `npx wrangler d1 migrations apply its-safety-portal-db
-- --remote` BEFORE any Worker build that reads/writes this table deploys — else the
-- schedule-task routes 500. Same rule as 0010/0033/0043/0053/0054/0059/0060/0066.
-- (Always `git pull` ~/its to latest main FIRST — the stale-migrations-list lockout
-- class, forensic #2.)

CREATE TABLE IF NOT EXISTS job_schedule_tasks (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  task_uuid             TEXT    NOT NULL UNIQUE,           -- stable identity across revisions (see header)
  job_id                TEXT    NOT NULL,                  -- soft ref → jobs.job_id (the 0031 posture)
  section               TEXT,                              -- phase context (Deliveries / Civil / …), NULL when none
  name                  TEXT    NOT NULL,
  match_key             TEXT    NOT NULL,                  -- Worker-computed, NOT unique (see header)
  duration_days         INTEGER CHECK (duration_days IS NULL OR (duration_days >= 0 AND duration_days <= 5000)),
  start_date            TEXT,                              -- YYYY-MM-DD (current, revision-movable)
  finish_date           TEXT,                              -- YYYY-MM-DD (current, revision-movable)
  baseline_start_date   TEXT,                              -- stamped at the task's OWN first commit, never rewritten
  baseline_finish_date  TEXT,                              -- stamped at the task's OWN first commit, never rewritten
  percent_done          INTEGER NOT NULL DEFAULT 0 CHECK (percent_done >= 0 AND percent_done <= 100),
  schedule_percent      INTEGER CHECK (schedule_percent IS NULL OR (schedule_percent >= 0 AND schedule_percent <= 100)),
                                                           -- what the last committed schedule asserted (PR-6 diff base)
  is_milestone          INTEGER NOT NULL DEFAULT 0,        -- zero-duration / same-day task
  is_contract_milestone INTEGER NOT NULL DEFAULT 0,        -- contract-flagged milestone (removal is BLOCKING at reconcile)
  is_delivery           INTEGER NOT NULL DEFAULT 0,        -- Deliveries-phase task (decision 5)
  delivered_date        TEXT,                              -- the delivered mark's business date (PR-5)
  delivered_by          TEXT,                              -- account username; W9 display-name-resolved on read
  delivered_at          INTEGER,                           -- unixepoch of the delivered mark (PR-5)
  predecessors_raw      TEXT,                              -- verbatim, never parsed
  sort_order            INTEGER NOT NULL DEFAULT 0,        -- document order (seq*10 at commit)
  source_row_index      INTEGER,                           -- the grid row this task came from (provenance)
  source_schedule_id    INTEGER,                           -- FK-ish → job_schedules.id of the FIRST commit
  updated_schedule_id   INTEGER,                           -- FK-ish → the last revision that touched it (PR-6)
  last_marked_by        TEXT,                              -- PORTAL MARKS ONLY (PR-5) — commits/edits leave NULL
  last_marked_at        INTEGER,                           -- PORTAL MARKS ONLY (PR-5)
  active                INTEGER NOT NULL DEFAULT 1,        -- soft delete (see header)
  created_at            INTEGER NOT NULL DEFAULT (unixepoch()),
  updated_at            INTEGER NOT NULL DEFAULT (unixepoch())
);
-- The Schedule page's read: active tasks in document order.
CREATE INDEX IF NOT EXISTS idx_job_schedule_tasks_job ON job_schedule_tasks(job_id, active, sort_order);
-- The PR-6 diff engine's lookup: incoming (section,name) → candidate tasks.
CREATE INDEX IF NOT EXISTS idx_job_schedule_tasks_match ON job_schedule_tasks(job_id, match_key);
