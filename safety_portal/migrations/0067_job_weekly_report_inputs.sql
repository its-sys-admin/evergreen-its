-- 0067 — the office half of the client-facing Weekly Production Report (operator ask, 2026-08-11).
--
-- CONTEXT: `progress_weekly_generate` compiles a packet of the week's daily-report PDFs and
-- `progress_send` emails it to the client. The client-facing artifact Evergreen actually uses is a
-- 5-page branded "Evergreen Weekly Production Report" (the ~/Desktop/WPRs corpus). Most of that
-- report is derivable from D1 — weather and crew narrative from `submissions.payload_json`, hours
-- from `time_entries`, deliveries from `material_receipt_events`, %-complete from the ADR-0006
-- `job_schedule_tasks` once that lane lands. THREE sections are NOT derivable and never will be:
--
--   1. Project Safety Status — the six OSHA case counts (lost-time / lost-work-days / job-transfer
--      / near-miss / other-recordable / first-aid, monthly AND project-to-date). `incident-report-v3`
--      captures location, description, witnesses and action-taken; it carries NO OSHA case
--      classification and no lost-work-days count, so the table cannot be computed from filings.
--   2. Labor Report by COMPANY — `personnel` has name/username/trade/current_job/created_by and no
--      employer column, so `time_entries.hours` cannot be attributed to a subcontractor. The only
--      company signal is the foreman's typed `crew_progress.crew_subcontractor` (headcount, not
--      hours).
--   3. Pending RFIs / Submittals / IFC Review / Change Orders — ITS tracks none of these anywhere.
--
-- Plus two judgment calls that must stay human: which photos represent the week to a client, and
-- whether a day counts as an INCLEMENT WEATHER DAY (a contractual delay claim, not a measurement —
-- deriving it from a precipitation number would manufacture a claim).
--
-- THIS TABLE is where the office's answers live: one row per (job_id, week_start), edited on the
-- portal's weekly-report screen, read by the Mac compile through
-- GET /api/internal/production-report. It holds ONLY what ITS cannot derive. Nothing here is
-- invented on the machine's behalf — an unfilled field renders blank, never a plausible default.
--
-- WHY JSON COLUMNS, not ~30 typed ones: every value here is display-only text destined for exactly
-- one document, never filtered, joined, or aggregated on. Typed columns would buy no query power
-- and would cost a migration every time the report template gains a line. This is the established
-- house shape for that case (`submissions.payload_json`, `daily_photo_pool.photo_json`,
-- `job_manifests.column_map_json`). The Worker validates each blob's shape on write; readers coerce
-- defensively because the blobs ride untrusted JSON transport to the renderer.
--
-- CARRY-FORWARD is a READ behaviour, not a stored one: a week with no row resolves against the most
-- recent EARLIER row for the same job, so a normal week is review-and-adjust rather than retype. The
-- carried values are returned flagged (`carried_from`) so the screen can show their provenance, and
-- nothing is written until the office actually saves. That keeps "the office never opened this week"
-- distinguishable from "the office reviewed it and changed nothing" — a distinction a
-- write-on-read seed would destroy.
--
-- photos_json is THREE-STATE and the distinction is load-bearing:
--   NULL  → the office has not curated; the route returns its own deterministic auto-selection.
--   '[]'  → the office explicitly chose NO photos; the report prints none.
--   [...] → the office's explicit ordered selection.
-- A NULL/'[]' collapse would make "I don't want photos this week" silently re-populate every
-- compile. Each entry stores `box_file_id` and `caption` ALONGSIDE `pool_id` deliberately: the
-- `daily_photo_pool` row is prunable (7-day unclaimed eviction, migration 0037) while the Box file
-- is the permanent record, so a selection made in week 1 must not dangle when the pool row ages out.
--
-- RETENTION: no time-based prune. This is a small record-like table (one row per job per week — a
-- 20-job shop makes ~1,000 rows a year) and it is the evidence of what was reported to a client.
-- It dies with its job via the purge-job cascade in index.ts, the same posture as
-- job_daily_requirements.
--
-- Writers: worker/fieldops_report.ts (session PUT, gated cap.jobtracker.manage).
-- Readers: worker/fieldops_report.ts (session GET for the screen; internal GET for the Mac compile).
--
-- MIGRATION NUMBER: 0066 is claimed by the in-flight ADR-0006 job-schedule lane (a parallel session,
-- its plan names "0066 next today"). This lane takes 0067 to avoid a merge collision; wrangler
-- tracks applied migrations by name, so a temporary gap is harmless if 0066 lands later.
--
-- DEPLOY ORDER (lockout class #2): apply this migration --remote BEFORE deploying the Worker that
-- SELECTs this table — a Worker deploy first would 500 the weekly-report routes. Always
-- `git -C ~/its pull` to latest main BEFORE `wrangler d1 migrations apply` (the stale-migrations-list
-- lockout class).

CREATE TABLE IF NOT EXISTS job_weekly_report_inputs (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id         TEXT    NOT NULL,                   -- FK-ish → jobs.job_id (route-validated)
  week_start     TEXT    NOT NULL,                   -- 'YYYY-MM-DD', the SATURDAY (shared/safety_week)

  -- {site_location, ess_management, mobilization_date, subcontractors[], prepared_by} — page-1 header
  header_json    TEXT    NOT NULL DEFAULT '{}',
  -- {lost_time:{month,to_date}, lost_work_days:{…}, job_transfer:{…}, near_miss:{…},
  --  other_recordable:{…}, first_aid:{…}} — the page-1 Project Safety Status grid
  safety_json    TEXT    NOT NULL DEFAULT '{}',
  -- {inclement_dates:['YYYY-MM-DD',…], weather_days_to_date:N} — page-2 human weather-day claim
  weather_json   TEXT    NOT NULL DEFAULT '{}',
  -- {rows:[{company,workers,man_hours}]} — page-2 Labor Report, seeded from crew_progress
  labor_json     TEXT    NOT NULL DEFAULT '{}',
  -- {critical_items, upcoming_activities, hazard_topics[]} — page-3 prose + page-1 meeting topics,
  -- each deterministically assembled then office-edited (NO AI anywhere in this lane)
  narrative_json TEXT    NOT NULL DEFAULT '{}',
  -- {rfis, submittals, ifc_review, change_orders} — page-5 Pending Requests
  pending_json   TEXT    NOT NULL DEFAULT '{}',
  -- NULL = auto-select (see the three-state note above); otherwise
  -- [{pool_id, box_file_id, caption, work_date}] in render order
  photos_json    TEXT,

  updated_by     TEXT,                               -- session username of the last saver
  updated_at     INTEGER,
  created_at     INTEGER NOT NULL DEFAULT (unixepoch())
);

-- One office record per (job, week). The UNIQUE is the upsert target for the session PUT and the
-- guard that two office users editing the same week converge on one row instead of forking it.
CREATE UNIQUE INDEX IF NOT EXISTS idx_job_weekly_report_inputs_key
  ON job_weekly_report_inputs(job_id, week_start);

-- The carry-forward lookup: "most recent row for this job STRICTLY EARLIER than this week",
-- served by a descending scan of the same (job_id, week_start) key. week_start is a zero-padded
-- ISO date, so lexical ordering IS chronological ordering — no date parsing in the query path.
