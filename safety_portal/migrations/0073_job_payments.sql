-- Job payment terms + cycles + receipts (ADR-0006 decision 10, PR-7) — the office-only
-- payment-tracking data model behind the Schedule page's Payments section.
--
-- DISPLAY-ONLY LANE: nothing here transmits anything, reminds anyone, or generates a
-- notice document. Payment STATE IS NEVER STORED — a pure derivation function
-- (worker/payments_derive.ts, server `today` in Pacific) computes draft / awaiting /
-- due_soon / overdue / nonpayment_notice_due / nonpayment_notice_sent /
-- suspension_notice_due / suspension_notice_sent / paid at read time, so a stored state
-- can never go stale or disagree with the dates that define it. The notice clocks key
-- off RECORDED notice dates only — the machine must not pretend a notice went out
-- (operator decision 3: notices are NEVER auto-sent; any future notice document rides a
-- *_Pending_Review sheet + the External Send Gate, Invariant 1, permanent).
--
-- COMMERCIALLY SENSITIVE (operator decision 4): cap.payments.manage is granted to
-- **admin ONLY** — invoice amounts, receipt history and escalation posture are the
-- office's business, and payment data appears in NO other route's response (the
-- fieldops-payments suite RED-proofs both).
--
-- APPLY BEFORE DEPLOY: run `npx wrangler d1 migrations apply its-safety-portal-db
-- --remote` BEFORE any Worker build that reads/writes these tables deploys — else the
-- /api/fieldops/payments routes 500, and resolveCapabilities (fail-closed) would 403
-- every admin until the capability row lands (the 0013/0044 activation rule). (Always
-- `git pull` ~/its to latest main FIRST — the stale-migrations-list lockout class,
-- forensic #2.)

-- Exact 0023/0044/0072 pattern: 0013's admin grant was a seed-time catch-all
-- (`SELECT key FROM capabilities`), so it does NOT auto-include a capability added
-- after 0013 — the admin grant here must be EXPLICIT. submitter/manager deliberately
-- get nothing (decision 4). INSERT OR IGNORE keeps a re-apply a no-op.
INSERT OR IGNORE INTO capabilities (key, label, description) VALUES
  ('cap.payments.manage', 'Payments (manage)',
   'Per-job payment terms, invoice cycles, receipts and the derived overdue/notice states on the Schedule page. Commercially sensitive — office/admin only (ADR-0006 operator decision 4); payment data appears in no other route''s response.');

INSERT OR IGNORE INTO role_capabilities (role_key, capability_key) VALUES
  ('admin', 'cap.payments.manage');

-- job_payment_terms — ONE terms row per job (the contract is per job, operator
-- decision 6; the same-client prefill copies the client's most recent job's terms at
-- CREATE time and never links them). billing_cadence powers TRANSIENT expected-next-
-- invoice reminders in the deferred alert fold-in — it NEVER auto-generates cycle rows
-- (ADR-0006 decision 10: cycles are manual, always).
--   • net_days                — invoice due = submitted + net_days (the Worker computes
--                               and STORES the snapshot on the cycle; see below).
--   • nonpayment_notice_days  — days OVERDUE at which a Notice of Nonpayment falls due.
--   • intent_to_suspend_days  — days AFTER the RECORDED nonpayment notice at which an
--                               Intent-to-Suspend falls due (a clock that starts only
--                               when a human records the notice date).
CREATE TABLE IF NOT EXISTS job_payment_terms (
  id                      INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id                  TEXT    NOT NULL UNIQUE,           -- soft ref → jobs.job_id (the 0031/0071 posture)
  billing_cadence         TEXT    NOT NULL DEFAULT 'monthly'
                          CHECK (billing_cadence IN ('monthly','semimonthly','milestone','other')),
  billing_cadence_detail  TEXT,                              -- free text ('15th + EOM', milestone names, …)
  net_days                INTEGER NOT NULL CHECK (net_days >= 0 AND net_days <= 365),
  nonpayment_notice_days  INTEGER NOT NULL CHECK (nonpayment_notice_days >= 1 AND nonpayment_notice_days <= 365),
  intent_to_suspend_days  INTEGER NOT NULL CHECK (intent_to_suspend_days >= 1 AND intent_to_suspend_days <= 365),
  notes                   TEXT,
  created_by              TEXT    NOT NULL,                  -- account username; never served raw (W9 posture)
  created_at              INTEGER NOT NULL DEFAULT (unixepoch()),
  updated_by              TEXT    NOT NULL,
  updated_at              INTEGER NOT NULL DEFAULT (unixepoch())
);

-- job_payment_cycles — MANUAL invoice/billing cycles (never auto-generated from the
-- cadence — decision 10). NO stored state column ON PURPOSE: every state above is
-- derived at read from these dates + the receipts below.
--   • invoice_submitted_date NULL = a DRAFT cycle (planned, not yet invoiced).
--   • due_date is a STORED Worker-computed SNAPSHOT (submitted + net_days AT SUBMIT
--     TIME): editing the terms later must NOT silently move an already-invoiced
--     cycle's due date — the invoice went out under the terms of its day (ADR-0006
--     decision 10). The snapshot recomputes ONLY when its OWN input (the submitted
--     date) changes through the edit route.
--   • suspend_notice_date REQUIRES nonpayment_notice_date to be set — enforced
--     in-WHERE by the notice route (an honest 409), deliberately NOT a CHECK: a CHECK
--     would also forbid an operator-CLI correction that clears the nonpayment date
--     while a suspend date stands, turning a data fix into a constraint crash.
CREATE TABLE IF NOT EXISTS job_payment_cycles (
  id                      INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_uuid              TEXT    NOT NULL UNIQUE,           -- stable handle (minted at create)
  job_id                  TEXT    NOT NULL,                  -- soft ref → jobs.job_id
  seq                     INTEGER NOT NULL DEFAULT 0,        -- display order (max+10 at create)
  label                   TEXT    NOT NULL,                  -- 'Mobilization', 'PP #3', … (≤120, route-bounded)
  invoice_submitted_date  TEXT,                              -- YYYY-MM-DD; NULL = draft
  invoice_amount_cents    INTEGER CHECK (invoice_amount_cents IS NULL OR invoice_amount_cents >= 0),
                                                             -- integer cents (the 0043 D8 rule — no floats)
  due_date                TEXT,                              -- STORED snapshot = submitted + net_days (see header)
  nonpayment_notice_date  TEXT,                              -- RECORDED by a human — starts the suspend clock
  suspend_notice_date     TEXT,                              -- RECORDED by a human; route-guarded (see header)
  note                    TEXT,
  active                  INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
  created_by              TEXT    NOT NULL,
  created_at              INTEGER NOT NULL DEFAULT (unixepoch()),
  updated_by              TEXT    NOT NULL,
  updated_at              INTEGER NOT NULL DEFAULT (unixepoch())
);
-- The Payments section's read: a job's active cycles in display order.
CREATE INDEX IF NOT EXISTS idx_job_payment_cycles_job ON job_payment_cycles(job_id, active, seq);

-- job_payment_receipts — APPEND-ONLY money-received events. Partial payments and
-- retainage holdbacks need NO schema change: each receipt is one event, the derivation
-- sums the active ones, and `balance_cents` falls out. A correction is deactivate +
-- re-add (the event history is the record — a receipt is a claim about money that
-- moved, never silently rewritten).
CREATE TABLE IF NOT EXISTS job_payment_receipts (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  cycle_id      INTEGER NOT NULL REFERENCES job_payment_cycles(id),
  received_date TEXT    NOT NULL,                            -- YYYY-MM-DD (the business date)
  amount_cents  INTEGER NOT NULL CHECK (amount_cents > 0),   -- integer cents; a zero receipt is no receipt
  note          TEXT,
  active        INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
  recorded_by   TEXT    NOT NULL,
  recorded_at   INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_job_payment_receipts_cycle ON job_payment_receipts(cycle_id, active);
