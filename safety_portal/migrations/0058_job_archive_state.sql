-- ROADMAP Track 6 — job archive state + the cap.job.archive grant.
--
-- Archiving a job relocates SIX per-job containers across TWO external systems (four Smartsheet
-- folders + two Box folders). That work runs asynchronously on the Mac-side daemon, so the portal
-- cannot treat "the operator pressed Archive" and "the folders have moved" as the same event —
-- which is exactly what a bare lifecycle flag would do. These columns are the state the UI reads
-- to tell the operator the truth while the relocation is in flight, and the record the daemon
-- resumes from when a cycle dies mid-sequence.
--
-- ORDER DEPENDENCY (activation): apply this to the live D1 BEFORE the Worker that references
-- cap.job.archive deploys. resolveCapabilities is fail-closed against these tables, so deploying
-- first would 403 every admin on the archive routes until the grant lands (the 0044 / 0051 rule).
--
-- Backfill is implicit: every existing row takes the defaults — archive_state='none',
-- archive_direction='', both timestamps NULL — i.e. "has never entered the archive workflow",
-- which is true of every job in the system today (the §51 move has never once fired live).

-- The state machine. Two columns rather than a ten-value enum: ONE shape, direction-tagged, so
-- "Archiving…" vs "Un-archiving…" is a render decision and every guard is written once.
--   archive_state     : none | requested | in_progress | complete | partial | failed
--   archive_direction : '' | archive | unarchive
ALTER TABLE jobs ADD COLUMN archive_state TEXT NOT NULL DEFAULT 'none';
ALTER TABLE jobs ADD COLUMN archive_direction TEXT NOT NULL DEFAULT '';

-- Epoch seconds. requested_at is set by the browser-facing route, completed_at by the daemon.
ALTER TABLE jobs ADD COLUMN archive_requested_at INTEGER;
ALTER TABLE jobs ADD COLUMN archive_completed_at INTEGER;

-- Consecutive failed attempts, incremented by the daemon on a partial/failed result. Lives in D1
-- rather than daemon memory so it survives a restart AND is visible in the UI — an operator can
-- see "tried 3 times" without reading logs. The pass stops auto-retrying at a cap; the operator's
-- "Try again" resets it to 0.
ALTER TABLE jobs ADD COLUMN archive_attempts INTEGER NOT NULL DEFAULT 0;

-- Daemon-written JSON: the per-container outcome list, so a PARTIAL archive is legible to a
-- Tier-2 operator ("Purchase Orders folder — did not move: <reason>") without opening logs.
-- Bounded at the write route; a blank string means "nothing reported yet".
ALTER TABLE jobs ADD COLUMN archive_detail TEXT NOT NULL DEFAULT '';

-- The per-job folder-name key, SNAPSHOTTED when the archive is requested.
--
-- This one is load-bearing and not obvious. Every per-job container is found BY NAME
-- (safety_naming.job_folder_name), and project_name is editable via /contacts
-- (fieldops_job_write.ts, added 2026-07-20). A rename between "operator pressed Archive" and "the
-- daemon reaches this job" would otherwise strand the relocation against a name that no longer
-- matches anything. Resolving against the snapshot removes that race entirely.
ALTER TABLE jobs ADD COLUMN archive_folder_key TEXT NOT NULL DEFAULT '';

-- The daemon's queue read: WHERE archive_state IN ('requested','in_progress') ORDER BY requested_at.
CREATE INDEX IF NOT EXISTS idx_jobs_archive_state ON jobs(archive_state, archive_requested_at);

-- cap.job.archive — its OWN capability, not an extension of cap.jobtracker.manage.
--
-- Every admin holds jobtracker.manage for routine create/rename/close work. Archiving is a
-- heavyweight, reversible, cross-system relocation; keeping it separate means it can be narrowed
-- later without also revoking day-to-day job management.
--
-- The admin grant must be EXPLICIT: 0013's admin grant was a seed-time
-- `INSERT ... SELECT key FROM capabilities` catch-all and does NOT auto-include capabilities added
-- after it (the 0044 / 0051 rule). manager and submitter get nothing.
INSERT OR IGNORE INTO capabilities (key, label, description) VALUES
  ('cap.job.archive', 'Archive jobs',
   'Archive / un-archive a job — relocates the job''s four per-job Smartsheet folders and both per-job Box folders into the ITS — Archive area, and moves them back. Separate from cap.jobtracker.manage on purpose: archiving is a heavyweight, reversible, cross-system relocation, not routine job editing.');

INSERT OR IGNORE INTO role_capabilities (role_key, capability_key) VALUES
  ('admin', 'cap.job.archive');
