# ITS one-shot migration scripts

This directory holds operator-invoked migration scripts — each runs once during
a PR's creation to land the live-data side of a code change (Smartsheet rows,
Box folders, etc.). All scripts here MUST be idempotent: re-running with the
target state already in place makes zero writes and exits 0.

## Scripts

### PO Box-root split (2026-08-11 family)

The PO lane's Box tree moved out of the safety root onto its own
`"ITS Purchase Orders"` root (`po_naming.CFG_BOX_PORTAL_ROOT`); the per-job folder
now holds the PO PDFs directly with `RFQs/` + `Vendor Quotes/` children.

- `relocate_po_box_folders.py` — plan-by-default, `--apply` + y/N to actuate.
  Three passes: (1) `<safety root>/<Job>/Purchase Orders` → `<PO root>/<Job>`
  (ONE atomic Box move+rename; a name collision under the PO root REFUSES that
  job, never merges); (2) moves the children of id-named folders (the
  `manifest_poll` job-id bug, e.g. `JOB-000031/Materials`) into the job's
  project-name folder, leaving the empty husk for manual Box-UI trash
  (`box_client` is deliberately MOVE-ONLY); (3) `<archive>/<Job>/Safety/Purchase
  Orders` → `<archive>/<Job>/Purchase Orders`, where the new
  `box:purchase_orders` archive slot's restore looks. Run AFTER
  `build_box_roots.py` has created the root + the ITS_Config row is seeded,
  BEFORE the lane files anything to the new root.
- `backfill_po_row_attachments.py` — plan-by-default, `--apply` + y/N. Attaches
  the already-filed Box PDFs/quote-forms inline on rows created before the
  attach-parity change (flat PO_Log, per-job "Purchase Orders"/"RFQs" mirrors;
  skip-if-present). Run AFTER the relocation (the quote-form lookup resolves
  `<PO root>/<job>/RFQs/`).

### Tenant wipe + orchestrated stand-up (2026-07-22 family)

The full-tenant lifecycle tools. `standup.py` supersedes the manual walk of the
builder sequence below for a from-zero stand-up — the individual builders remain
the units it runs, and each is still independently runnable.

1. `wipe_tenant.py` — name-guarded sandbox wipe (Smartsheet workspaces + Box
   roots, hard-coded name+id allowlist, daemon-down precondition, typed-phrase
   gate, dump-before-delete to `~/its/logs/migrations/prewipe_<UTC>/`; transient
   fetch errors retry via `_rest_retry` and ABORT on exhaustion — only permanent
   404/1006/1115 shells classify "unreadable"). Plan-only by default; `--commit`
   to execute. **There is deliberately NO production wipe variant**: rollback is
   repoint-back (`production_rollback.md` — the mirror Worker + workspaces stay
   intact), a botched partial stand-up RESUMES (idempotent builders +
   `--resume`), and the rare malformed-object deletion is a one-off name-guarded
   SDK script with a hard-coded allowlist, PR-reviewed — exactly the friction a
   destructive tool against production should carry.
2. `standup.py` — the orchestrator: runs every builder in dependency order as
   subprocesses (under the `STANDUP_NONINTERACTIVE` contract — closed stdin, an
   unexpected prompt fails the stage loudly), interleaves
   `sheet_ids_regen.py --write` between stages so every ID surface is flipped
   automatically — FLIP still precedes SEED, the flip is just mechanical.
   Restores data-bearing SoR rows + workspace share lists from the pre-wipe
   dump, auto-pastes the Box-root ids into ITS_Config, streams prefixed child
   output to a per-run transcript, persists run state (`--resume` restarts at
   the first incomplete stage; `--start-at` stays the manual override), and
   finishes with `sheet_ids_regen.py --check` + `verify_cutover --only config`.
   **Run-branch mode (default ON; `--no-run-branch` opts out):** each run
   checks out `standup/run-<UTC>` (refusing a dirty tree — repo files only,
   `logs/` never counts), commits a checkpoint after every stage that changes
   repo files (`:(exclude)logs` — the prewipe dumps are untracked-not-ignored
   and must never land on the branch), `--resume` fetches+merges `origin/main`
   so a mid-run fix PR is picked up (conflicts surface and STOP, never
   auto-resolved), and completion pushes the branch + prints the landing
   `gh pr create` command.
   `standup.py finish` is the post-merge epilogue (state cleanup → DARK fleet
   reload → heartbeat wait → error sweep → read-only gate-flip worksheet →
   dashboard restart LAST) — see `docs/runbooks/tenant_standup.md`.
3. `sheet_ids_regen.py` — the circle-closer, also useful standalone:
   `--check` is a read-only parity probe (every constant resolves live to an
   object of the expected name); `--write` regenerates the ID surfaces
   (incl. `docs/doctrine_manifest.yaml`'s canonical ids) and sweeps
   *.py/yaml/md for surviving old ids (report-only outside the remap scope).
4. `build_legacy_workspaces.py` — builders for the four hand-created workspaces
   that never had one (Human Review, Operations master DBs, Archive, the
   Forefront demo skeleton + week-folder template sheets).

### Production cutover tools (Aug-3 family — build-tested, Seth-attended to run)

- `seed_production_shares.py` + `production_shares_manifest.json` — the CL-11
  F22 approver-share applier: manifest-driven (data file; the emails live there
  because the CI identity guard blocks them in .py), PLAN default, ADD-only
  (never unshares), OWNER-check, y/N gate; the Ezra-typo class refuses at
  manifest load. Verify half: `verify_cutover --only approver-shares` (VC-10).
- `production_repoint.py` + `production_repoint_map.json` — the CL-12 repoint
  actuator: applies `production_repoint_changeset.md` §A–D from reviewed data,
  PLAN default, typed-phrase `--commit`, DRIFTED-row refusal before the first
  write, §E gates structurally excluded (an allowlist of the reviewed A–D
  setting classes — the tool cannot carry a gate flip). Box roots resolved
  live, never typed.

### Appendix — standalone builder reference (superseded as a SEQUENCE by standup.py)

**The manual walk below is RETIRED as the cutover procedure** — `standup.py`
runs the full dependency order with the FLIP mechanized (`sheet_ids_regen.py
--write` between stages; the old "hand-paste the printed ids" steps are dead).
Each builder remains independently runnable for a one-off repair; this appendix
records the standalone semantics:

- `build_system_workspace.py` — the "ITS — System" workspace + its four folders.
- `build_system_sheets.py` — the five System sheets (resolves folders by NAME).
- `build_safety_portal_workspace.py` — the "ITS –– Safety Portal" workspace +
  `00_Safety Portal` / `00_Form Catalog`.
- `build_box_roots.py` — the four Box roots (Safety, Progress, Purchase Orders,
  Archive). Its output does NOT go into `shared/sheet_ids.py`: the folder ids
  land in the ITS_Config `*.box.portal_root_folder_id` /
  `*.box.archive_root_folder_id` rows — AUTO-PASTED by standup's box-roots
  stage when orchestrated (a standalone run pastes them by hand). Requires Box
  OAuth as the dedicated ITS identity first (`scripts/setup_box_oauth.py`).

Each builder is create-only, idempotent, live-by-default with a y/N confirmation
(auto-approved ONLY under standup's `STANDUP_NONINTERACTIVE` contract), and
takes `--dry-run`. Reconcile any `[WARN]` duplicate-name ambiguity BEFORE
resuming an orchestrated run.

### `box_clone_1111a_to_projects.py`

Clones the `1111A (Copy for new projects)` Box template into the 6 Forefront
project folders (Bradley 1/2, Brimfield 1/2, Huntley, Rockford) under `ITS DATA`.
Closes the last Box-side prerequisite for R3 session 1 (intake.py wiring) by
materializing the Box-side targets referenced from `shared.defaults.BOX_PROJECT_FOLDERS`.
Ran 2026-05-21.

The Box source-folder lock gotcha motivated the script's retry pattern: Box's
async deep-copy holds a server-side lock on the source folder for the duration
of the operation; subsequent copies (UI or API) from the same source fail with
HTTP 500 + a "locked" message until the lock clears. Lock duration is variable
(observed ~30s to several minutes for the 269-file / 14-subfolder template).
`copy_with_lock_retry` waits 30s between attempts up to 40 attempts (20-minute
total budget per copy); hammering the queue does not speed it up. The 6
resulting folder IDs are committed in `shared.defaults.BOX_PROJECT_FOLDERS`.

Idempotent: a re-run with all 6 folders present makes zero copy calls, prints
6 `EXISTS` lines, exits 0.

### `seed_safety_intake_config.py`

Seeds 5 `safety_reports.intake.*` rows in `ITS_Config` (workstream `safety_reports`)
that `safety_reports/intake.py` reads at runtime: `allowed_senders` (JSON list),
`classification_model` (Anthropic model ID), `box_filing_enabled` (capability flag),
`review_queue_on_low_confidence` (behavior flag), and `confidence_threshold` (float).
Ran 2026-05-21. Idempotent per row.

### `seed_safety_intake_polling_config.py`

Seeds 3 `safety_reports.intake.*` polling-daemon rows in `ITS_Config` (workstream
`safety_reports`) originally consumed by `safety_reports/intake_poll.py` (the email
poller, RETIRED 2026-06-05 and DELETED 2026-07-03) + the install script. The rows are
kept — and the seeder stays in `standup.py`'s seeders stage — for install-script plist
substitution and restore/row-shape parity; a resurrected email poller re-reads them:
`poll_interval_seconds` (read at install time and substituted into the launchd
plist's `StartInterval`), `mailbox` (Graph mailbox to poll), and `polling_enabled`
(per-workstream kill switch, distinct from the global `system.state`). Companion
to PR #59. Idempotent per row.
