-- Schedule field mark-off capability (ADR-0006 PR-5) — cap.schedule.mark, the capability
-- behind POST /api/fieldops/schedule-tasks/:id/{progress,milestone-done,delivered}
-- (worker/fieldops_schedule_tasks.ts): the field's quick-% chips, the milestone done-mark
-- and the Deliveries-task delivered mark on the per-job Schedule page.
--
-- WHY SUBMITTER GETS IT: the person marking schedule progress IS the field PM on the job —
-- the same person who receives materials, and 'submitter' is that role's key (relabeled
-- 0027). This mirrors cap.materials.receive (0013 granted it to submitter for exactly this
-- reason; 0023 extended it to manager). The grant is deliberately permissive-by-role and
-- CONFINED BY SCOPE: every mark route resolves the task's job and runs requireJobScope, so
-- a non-admin can only mark tasks on the job that is their own placement
-- (personnel.current_job) — 403 forbidden_job otherwise; cap.jobtracker.manage (admin) is
-- the bypass set. Tightening the audience later is a role_capabilities grant-row DELETE
-- (capabilities resolve per-request, fail-closed — the vestigial-caps suite proves a revoke
-- bites immediately), no code change.
--
-- Exact 0023/0044 pattern: 0013's admin grant was a seed-time catch-all
-- (`SELECT key FROM capabilities`), so it does NOT auto-include a capability added after
-- 0013 — the admin grant here must be EXPLICIT. Enumerated explicitly (never
-- `SELECT … FROM roles`) so the grant is exact + auditable.
--
-- ORDER DEPENDENCY (activation): apply to the live D1 BEFORE the Worker carrying the mark
-- routes deploys — resolveCapabilities is fail-closed, so the routes would 403 every role
-- until this lands (the 0013/0044 activation rule). INSERT OR IGNORE keeps a re-apply a
-- no-op. (Always `git -C ~/its pull origin main` FIRST — the stale-migrations-list lockout
-- class, forensic #2.)

INSERT OR IGNORE INTO capabilities (key, label, description) VALUES
  ('cap.schedule.mark', 'Schedule (mark progress)',
   'Field mark-off of schedule tasks on the per-job Schedule page: percent done, milestone done, delivery received. Per-job scoped in-route (own placement only; cap.jobtracker.manage bypasses).');

INSERT OR IGNORE INTO role_capabilities (role_key, capability_key) VALUES
  ('admin',     'cap.schedule.mark'),
  ('manager',   'cap.schedule.mark'),
  ('submitter', 'cap.schedule.mark');
