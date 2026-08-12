---
type: reference
status: active
workstream: docs
tags: [standards, lessons, house-reflexes, canonical]
---

# ITS House Reflexes — the standards that keep us honest

**Purpose.** The single, canonical, deduped home for the recurring lessons and working standards of ITS —
so a fresh Claude Code session loads them once and *doesn't let the standards fall.* Each entry is a rule +
the pattern that earned it. This is **execution-facing** (how to work in `~/its`); the **invariants and
doctrine** it points to are canonical in `~/its-blueprint/doctrine/` (Op Stds v20, Foundation Mission v11) —
planning-layer wins. The **roadmap** is canonical in the blueprint + the field-ops program file; this is not
that. Loaded via `@import` from `CLAUDE.md`'s START-HERE block.

> When you add a lesson: add it *here* (one line + why), not in a new doc or a fifth memory file. If it's
> doctrine-level, it belongs in the blueprint doctrine instead. This file is the "don't sprawl" backstop.

---

## 1 — Trust the live code, never the claim

- **A current-state claim is a hypothesis until verified against live HEAD.** A brief / audit / memory / chat
  note that names a file, function, line, SHA, PR, or sheet-ID has drifted between authorship and now —
  `grep`/`Read` the real code, `gh` the real PR, before acting. **Zero grep hits is decisive over confident
  memory.** (Forensic class #3, recurred 16×. The `brief-validator` agent automates this.)
- **A datum has N implementations — enumerate ALL of them first.** "Fixed in one place" is the recurring
  incomplete-fan-out bug: a PDF name lives in the Box file + the Smartsheet attachment + the Worker
  `Content-Disposition`; a status value lives in the writer const + `picklist_validation.REGISTRY`. `grep` the
  datum everywhere and **live-test** before claiming done. (Multi-surface fan-out.)
- **Adding a package / daemon / secret / config-row / workstream-tag reconciles ALL its registries in the SAME
  PR (definition-of-done).** The recurring "added the thing, forgot a registry" miss. A new **package** → a
  CLAUDE.md "What's stubbed vs. real" row + `generate_config_dictionary._SCAN_ROOTS` (then regen the config dict
  + re-record its enablement-manifest sha256). A new **secret** → `verify_cutover` VC-01 + the host-migration A5
  table. A new **load-bearing ITS_Config row** → `verify_cutover` VC-03 (`non_empty`/sandbox-scan; never forced
  `true` for a dark gate). A new **daemon** → a plist + `install.sh` + `TRACKED_JOBS` + `ITS_Daemon_Health`. A
  new **workstream tag** lives in **THREE** copies — `docs/operations/doc_conventions.md`,
  `docs/doctrine_manifest.yaml workstream_tags`, AND `scripts/lint_doc_conventions.CANONICAL_WORKSTREAMS` (the
  2026-07-12 WP1 pass updated the first two and MISSED the third — this reflex exists because of it). A new
  `StrEnum` value → `picklist_validation.REGISTRY`. A new generation/send script → the capability-gating lists.
  `grep` the datum across every surface before claiming done.
- **The enablement sha-pin covers `docs/references/` and `docs/troubleshooting/` too — not just
  `docs/enablement/`.** `docs/enablement/manifest.yaml` pins **22** sources by sha256, and **10 of them live
  outside `docs/enablement/`**: nine `docs/references/*.md` (`its_config_dictionary`, `system_architecture`,
  `daemon_reference`, `data_model_reference`, `integration_reference`, `security_trust_model`,
  `escalation_matrix`, `glossary`, `documentation_index`) plus `docs/troubleshooting/troubleshooting_guide.md`.
  Editing any of them without re-recording its sha turns the `test_docs_pdf` docs-currency gate RED, and nothing
  about the filename hints that it is pinned. There is no `--record` flag — `scripts/build_docs_pdfs.py --check`
  only *reports* drift; re-record by hand (`shasum -a 256 <file>` into the matching `sha256:` line) and re-run
  `--check`. `its_config_dictionary.md` is doubly bound: it is GENERATED, so edit
  `scripts/generate_config_dictionary.py`, regenerate, *then* re-record.
- **Don't deploy / migrate / audit from a stale checkout.** `git -C ~/its pull origin main` before any
  `wrangler deploy`, `wrangler d1 migrations apply/list`, or cross-repo drift audit — a 25-commit-behind tree
  reported "No migrations to apply" while the live Worker expected the new tables → universal lockout.
  (Forensic class #2; `block-stale-cloudflare-deploy.sh` + watchdog Check Q/S catch the in-session case.)

## 2 — Prove the control bites (green proves nothing)

- **A new test / hook / gate is worthless until it RED-lights on a synthetic violation.** Inject → confirm it
  fails → revert. For anything that shells out or hits an SDK, add a **live smoke** on top. (feedback: prove-the-control-bites.)
- **Mandatory live smoke before merge for new shared infrastructure.** Mocks-pass-but-live-API-rejects is a
  recurring class (SimpleNamespace mocks miss what the real Smartsheet/Box/Graph SDK rejects). (feedback: mandatory-live-smoke; Op Stds §30.)
- **Adversarial review is definition-of-done on any trust-boundary surface** — an untrusted parse/decode, a
  D1/Smartsheet write-route fed by client/operator data, or an external-send path. Unit tests structurally
  *cannot* find injection, double-send windows, or fail-open misconfig; adversarial review (`/security-review`,
  `portal-worker-security-reviewer`, `ops-stds-enforcer`) repeatedly has. (Forensic classes #9/#14.)
- **A textually-clean auto-merge is not semantically proven** — re-run the FULL CI gate on the *rebased* tree,
  never trust a conflict-free rebase of overlapping PRs.
- **After `git add`, verify intended NEW files are actually TRACKED — a green CI on a missing test proves
  nothing.** `.gitignore` can silently swallow a file: a `*secret*`/`*key*`/`*.pem`-named **test** matches a
  credential-blocking ignore rule, so `git add -A` drops it, `git status` says "clean", and CI runs green
  because the test *was never collected* — the control ships absent while the manifest claims `enforced`.
  `git ls-files <path>` (or `git ls-tree HEAD`) is the proof, not `git status`. (Bit the 2026-07-09 §54
  backstop: `tests/test_secret_leak_backstop.py` matched `.gitignore *_secret*`; caught by adversarial
  review, not the "green" gate. Fix: rename to dodge the pattern, e.g. `…_redaction_backstop.py`.)

## 3 — Git / worktree / deploy discipline (the live-tree is a loaded gun)

- **The launchd daemons run the `~/its` working tree from disk every ~60s.** Uncommitted Python-SOURCE edits
  go live immediately; committing in `~/its` mid-cycle can strand the publish daemon on a `publish/req-*`
  branch. **Any Python-source change → a per-task worktree off `origin/main` with its OWN fresh venv**
  (`python -m venv .venv-wt && pip install -e '.[dev]'`; NEVER `cp -R .venv` — the copied `bin/pip` shebang
  repoints the live editable install). **Docs-only edits are fine on the live tree.** (worktree_discipline.md.)
- **Never two doctrine-touching sessions on one blueprint checkout** — isolate blueprint work in its own worktree.
- **The four-part PR-landing verify** (before believing a PR landed): `state=MERGED` · `mergedAt` non-null ·
  `mergeCommit.oid` present · **main-branch CI on the merge commit = SUCCESS**. Passing 1–3 but failing 4 is
  *functionally not landed*. (`pr-landed-verifier`; docs/operations/pr_merge_discipline.md.)
- **Squash-merge repo: PR `state=MERGED` is the ONLY safe branch-delete signal** (commits-ahead misleads).
  `git branch -D` is hook-blocked → `git update-ref -d refs/heads/<b>` *after* the MERGED verify. Preserve
  OPEN / CLOSED-unmerged / no-PR branches.
- **CI double-triggers (push + pull_request); a check-run can stick IN_PROGRESS on a MERGEABLE/CLEAN PR.**
  Verify via run-level conclusion + `mergeStateStatus`, never `gh pr checks --watch`. GitHub GraphQL/Actions
  writes flake with 401s and CodeQL infra-fails → merges land `unstable`; use REST `mergeable_state`, retry
  writes in a loop, merge on `unstable` when only CodeQL-infra is red.
- **Auto-merge is OFF** — the publish daemon polls `mergeStateStatus` then `--squash`; `_reset_to_main`
  recovers a stranded tree (not `git stash`).

## 4 — The invariants are load-bearing (cite doctrine, don't reinvent)

- **Invariant 1 — External Send Gate (permanent).** Two-process: generation scripts have ZERO send capability,
  send scripts have ZERO AI. Enforced at import by `tests/test_capability_gating.py` — enroll every new
  generation/send script. The kill switch is a fail-open operator convenience, **not** the security boundary.
- **Invariant 2 — Adversarial Input.** All content outside the operating tenant is untrusted data; wrap with
  `untrusted_content`, screen attachments/photos (§34), run `anomaly_logger.check()` before trusting an
  extraction. Layer 5 (anomaly logging) is a **post-hoc tripwire, not a barrier** — prevention is Layers 2–4 +
  the send gate.
- **Picklist REGISTRY parity:** a new `StrEnum` value that a route writes MUST be added to
  `shared/picklist_validation.REGISTRY` in the *same* PR — every `update_rows` is gated on it; mocks never
  catch a miss, only a live smoke does. (CI gate now enforces parity.)

## 5 — Config / state / data discipline

- **Observable config resolution:** log each resolved setting with its source (`ITS_Config` vs `default`) at
  startup and WARN-loud on a missing declared key. A silent fallback to a hardcoded default hides a real
  misconfig — "never silent" applies to config too. (Forensic class #7; `REQUIRED_CONFIG` pass = issue #336.)
- **Never pin EDITABLE-config CONTENT in a test — assert shape / round-trip / served-equals-source.** The §50
  config editor auto-merges purchaser/tax/terms edits on green CI, so a test that hard-pins the live content
  (an entity string, an invoice email, an absolute `config_version`/`current_version`, an exact `rates_bp`
  table, or cents-math derived from a live rate) RED-lights the instant the operator edits it and permanently
  strands the edit PR (`_wait_for_ci` never advances). This is a *self-defeating* test class that has now
  recurred on two §50 actuators (form-publish catalog counts #222/#228, then the config editor #511). Instead:
  seed FIXED fixtures at a non-1 sentinel and assert versions RELATIVE (`new == seed + 1`); derive Worker-side
  expectations by importing the SAME bundled JSON (`po.test.ts` → `../../po_materials/config/*.json`) so math
  tracks the config; and assert served-config EQUALS the imported source (drift check), never a literal.
  Reference pattern: `tests/test_config_apply.py` fixed fixtures + `tests/test_po_terms.py` shape +
  `safety_portal/test/po.test.ts` derive-from-source. (SPA config tests stay MOCKED — keep their fixtures
  hardcoded, never `importActual` the live config.)
- **`ITS_Config` reads are workstream-scoped** — `get_setting(key, workstream=…)` matches on the Setting name
  AND the Workstream cell. Footgun: the progress intake gate `progress_reports.intake_enabled` is read under
  `Workstream=safety_reports` (intake's own workstream), not `progress_reports`.
- **A dark-shipped gate has NO row to flip — SEED the row (even `=false`) when the code merges.** A boolean
  `ITS_Config` gate read via `_read_bool_setting(default=False)` treats a MISSING row identically to `false`,
  so a capability that "ships dark" has *no row at all* — the operator hunts for a switch that doesn't exist.
  Seed the gate row (value `false`) in the same change that adds the gated code, so activation is a visible
  cell-flip, not a phantom. (Bit the 2026-07-05 equipment/materials activation: `equipment_enabled`/
  `materials_enabled` had no row → the operator couldn't find one to flip → the rows had to be CREATED.)
- **Static text must never assert a LIVE gate state — say what the switch MEANS, not what it is set to.**
  A doc, a table row, a code comment or a config-editor note that says "ships dark" / "currently off" is
  redundant the day it is written (the value is one read away) and wrong the day someone flips it. It went
  wrong at scale: on 2026-07-21 **every** procurement send gate read `true` — `po_send`, `rfq_send`,
  `subcontract_send`, `estimate_poll`, `rfq_poll`, `subcontract_poll` — while CLAUDE.md's "What's stubbed
  vs. real" table, the troubleshooting tree, the auto-memory and the config-editor notes all still said
  "ships dark", and the `rfq_send` ITS_Config row's own Description still listed unmet go-live
  preconditions beside a `true` value. A Tier-2 operator following any of them hunts for a switch that is
  already thrown. Write the SEMANTICS ("pause anytime; turning ON escalates") and point at ITS_Config as
  the single source of live state; if you must date-stamp an observation, mark it as an observation.
  `tests/test_config_editor.py::test_no_registry_note_asserts_a_live_gate_state` enforces this for the
  editor's own notes — docs have no such guard, so they need the discipline.

- **Read a gate row's full Description BEFORE flipping it — a doctrine-divergent gate flip is a doctrine
  action (§44 high-class), not an autonomous one.** A gate's `ITS_Config` Description cell can carry an
  explicit precondition ("Do NOT set true until the §51 rider is merged"). A verbal go-ahead resolves the
  *decision* but not the *documented precondition* — flipping a capability whose activation contradicts
  canonical doctrine introduces a code-vs-doctrine drift the auditor flags, and doctrine is a FIXED
  high-capability class that escalates, never gets actioned unsupervised. Fetch + read the row's cells,
  not just its rowId, before an `update_rows` flip. (Bit the 2026-07-06 M3 activation: `materials_enabled`
  was flipped on a verbal one-way-up call, then reverted when the response revealed the in-cell "rider must
  be merged first" guardrail — `incidents_enabled`, which has no such block, stayed on.)
- **Flip a daemon's `ITS_Config` polling gate only AFTER its matching Cloudflare Worker secret/route is
  deployed.** Flipping the gate first produces a benign-but-noisy `bearer_rejected`/401 CRITICAL storm on
  every cycle until the deploy catches up — and the storm looks like a real auth incident, so someone burns
  a chase on it. Root-caused on a subcontract-lane `bearer_rejected` during the 2026-07-14 error-chase;
  apply the ordering to every config-actuator / Worker-secret pairing.
- **Never `Path.write_text/write_bytes` under `~/its/state/`** — route every state write through
  `shared/state_io.py` (`atomic_write_json/text`, `with_path_lock` on a sidecar `.lock`). Enforced at CI
  (`test_state_write_discipline.py`).
- **Display-name-only attribution:** crew/task/report-facing WHO fields resolve through `personnel.name`,
  never `users.username`. (Caught 3× — P2.6, R1, R7.)
- **D1 mutation + its audit row are ONE atomic `db.batch([...])`** (the "W4" class) — never a mutate-then-audit
  two-step.

## 6 — Roadmap & scope discipline

- **New scope slots into the roadmap, it is not built ad-hoc.** When an idea surfaces mid-phase: scope it,
  queue it at the right staged slot, finish the current phase, build only on green-light. (feedback: slot-into-roadmap.)
- **Don't harden dormant subsystems.** Before cleaning a tech-debt item, gate on "live consumer + real data?"
  — not just "collision-safe" or "still open." (feedback: dont-harden-dormant.)
- **Prefer simple-correct over premature optimization** for an unverified constraint; match a sheet's storage
  period to its report cadence (safety/progress stay weekly). (feedback: match-period-to-cadence.)
- **Parallelize genuinely-independent work** (no file overlap, own worktree, no shared D1/deploy) with a
  background Agent/Workflow; serialize only on real dependencies or shared resources.

## 7 — Known platform gotchas (the ones that have bitten us)

- **Keychain `security … -w` TTY trap:** with a controlling `/dev/tty` present it reads the terminal and
  *ignores piped stdin* — corrupted the Box refresh token twice. Use `-w VALUE` / run headless. (`keychain.set_secret` now detects TTY and handles both.)
- **Cloudflare `custom_domain: true` disables the `*.workers.dev` URL on deploy** (error 1042) unless
  `workers_dev: true` is also set — repoint daemon base-URLs to the custom domain right after deploy.
- **Worker `ASSETS.fetch()` responses have immutable headers** — mutating them (`secureHeaders`/`c.header`)
  throws → 500s every asset + SPA doc under `run_worker_first:true`. Reconstruct the response; verify with
  `wrangler dev` (vitest can't serve assets).
- **Worker SPA fallback returns 200 (index.html) for a deleted/missing asset path** — verify asset removal by
  content-type, not status.
- **Smartsheet MCP = `delete_rows` only** (no delete_sheet/folder/workspace) — use the Python SDK for
  sheet/folder deletion, name-guarded with a hard-coded allowlist. Sheet names cap at 50 chars (errorCode
  1041; folders uncapped). `ABSTRACT_DATETIME` accepts naive `YYYY-MM-DDTHH:MM:SS` only (write naive Pacific
  wall-clock).
- **Box MCP has NO delete** — use `box_client` (the ITS OAuth account). Refresh tokens rotate every exchange;
  `_store_tokens` MUST persist the new one or Box dies in ~60 days.
- **Mirror-loop re-creation (2026-07-03):** `purge-job` deletes D1 only; if you purge a portal job while its
  row still exists in `ITS_Active_Jobs`, the `portal_poll` down-sync re-inserts it as `origin='smartsheet'`.
  **Delete the `ITS_Active_Jobs` row FIRST, then purge.** (No automated cleanup of the Smartsheet folder /
  week-sheets / Box PDFs on any removal path — a full "nuke a job everywhere" is a manual 3-system op.)
- **`deploy "nothing changed"` is usually browser cache** — confirm the live asset hash changed in the deploy
  output, then hard-refresh / incognito; don't chase it as a deploy failure.
