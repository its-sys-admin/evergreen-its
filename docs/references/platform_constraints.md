---
type: reference
date: 2026-08-10
status: active
workstream: null
tags: [reference, platform-constraints, smartsheet, box, cloudflare, powershell, tier-2]
---

# Platform constraints and vendor gotchas

Permanent behaviours of the platforms ITS builds on — Smartsheet, Box, Cloudflare, Exchange
Online — plus a few deliberate ITS design choices that read like defects until you know why.

**Nothing here is work.** These entries have no fix, because there is nothing on our side to
fix: a vendor API rejects a shape, a feature exists only in a web UI, a transient resolves on
retry. They live here so a future session can look up the constraint instead of rediscovering
it, and so `docs/tech_debt.md` can mean "work someone could actually do".

They were moved out of `tech_debt.md` on 2026-08-10. That file had grown to 290 KB against its
own 256 KB cap, and a full triage of all 133 entries found the cause was not unfixed work:
11 entries were permanent constraints like these, and a further 12 were stale or duplicated
narrative. Counting a platform restriction as open debt overstates the backlog forever, because
it can never close.

> **If you are looking for open work, this is the wrong file** — see `docs/tech_debt.md`.
> **If something here ever becomes fixable** (a vendor ships an API for it), move it back.

---

## Smartsheet

### Smartsheet API constraint: column FORMAT must be set via model attribute, not dict constructor [OPEN 2026-06-07]

**Verified live (PR #187, 2026-06-07).** When using the Smartsheet Python SDK to create or update a column, the column **format string** (font, size, bold, color, etc.) must be assigned via the model **attribute** (`column.format = "..."`) — passing `format` as a key in the dict constructor (`smartsheet.models.Column({"format": "..."})`) silently drops the value. Column **width** works via either path (dict or attribute). The same per-cell format DOES work via the `Cell` dict constructor (`_resolve_cells` attaches it via the `_formats` meta-key extension).

**Palette index source:** `GET /2.0/serverinfo` → `.formats.color` (array, index → hex). Verified live: 38 = `#237F2E` (dark green), 7 = `#E7F5E9` (light green), 18 = `#E5E5E5` (gray). `dateFormat` enum at `.formats.dateFormat`. Format-descriptor positions: 2=bold, 8=textColor, 9=backgroundColor, 16=dateFormat.

**Impact:** code that sets a column format via the dict constructor silently succeeds (200) but the column stays unformatted. Always use the attribute path for column format.

**Tag:** `smartsheet`, `sdk-vs-live`, `styling`. **Revisit when:** any new column-format code; `smartsheet_client.apply_column_styles` already uses the attribute path.

### Smartsheet API constraint: DATETIME columns require system column type [OPEN]

Discovered 2026-05-17 evening while provisioning `ITS_Errors`, `ITS_Quarantine`, and other sheets. The Smartsheet "Create Sheet" endpoint accepts `DATETIME` columns only when paired with `systemColumnType: MODIFIED_DATE | CREATED_DATE`. User-defined DATETIME columns (e.g., "Timestamp", "Surfaced At", "Resolved At", "Received At", "Reviewed At") are rejected with a generic HTTP 500 / error code 4000 and no descriptive message.

**Workaround:** Use `DATE` for all user-defined date columns. Time-of-day precision is lost from the in-sheet representation.

**Mitigation:** Smartsheet's intrinsic row-level `created_at` (and `modified_at`) attributes are full datetimes and are queryable via the API. Code-side ordering and time-of-day inspection use those fields rather than the in-sheet DATE columns. The in-sheet DATE columns serve human readability; the intrinsic timestamps serve programmatic precision.

**Revisit when:** Smartsheet API surfaces user-editable DATETIME columns, or a workstream finds DATE-only resolution genuinely insufficient and the `created_at` fallback isn't viable for the use case.

_Update 2026-06-09 (PR #245 WSR Approved At / Sent At sweep):_ `ABSTRACT_DATETIME` (the "Date/Time" user type in the Smartsheet UI) **CAN** be created/retyped to via `update_column` and accepts a **naive** `YYYY-MM-DDTHH:MM:SS` value (stored/displayed literally). A plain `DATETIME` column is still rejected with errorCode 4000 — that restriction stands. `ABSTRACT_DATETIME` rejects any offset or 'Z' suffix (errorCode 5536). Existing DATE-only cells coerce to midnight on retype to ABSTRACT_DATETIME. The `WSR_human_review` sheet (id `5035670127988612`) columns "Approved At" (col `7944658226548612`) and "Sent At" (col `5129908459442052`) were live-retyped DATE → ABSTRACT_DATETIME, confirming the above. Write naive Pacific wall-clock (operator preference).

### Smartsheet API constraint: AUTO_NUMBER columns rejected at sheet creation [OPEN]

Discovered same session. `systemColumnType: AUTO_NUMBER` is rejected at the "Create Sheet" endpoint, whether or not the column is primary, with or without an `autoNumberFormat` config. Other system column types (`MODIFIED_DATE`, `MODIFIED_BY`) are accepted in the same payload — so the rejection is specific to AUTO_NUMBER, not a generic system-column-at-create issue.

**Workaround:** Each system sheet's primary column is a plain `TEXT_NUMBER` that code populates with a descriptive label ("Error", "Quarantined Message", "Entry"). Smartsheet's intrinsic row IDs serve as the unique identity for any code-side references.

**Mitigation:** Code-side row references use the Smartsheet row ID (returned in every API response). The human-readable primary column gives operators a meaningful label in the UI without needing auto-numbering.

**Revisit when:** A workstream requires user-visible auto-IDs (e.g., a customer-facing ticket number) and the code-populated label pattern is insufficient. Likely never — the intrinsic row IDs cover the technical need and labels cover the human need.

### Smartsheet UI-only constraints (Forms, CF, Filter Views, Restrict-to-dropdown) [OPEN]

Several Smartsheet features are exposed only through the Smartsheet web UI and have NO REST/SDK surface — meaning Claude Code can NOT provision, audit, or sync these per-customer settings during deployment. Operator must configure each manually at deployment time and document the choices.

The known UI-only surfaces (as of 2026-05):

- **Form creation + configuration** — `Smartsheet → Forms` panel. Forms are the primary intake surface for several workstreams; no API equivalent. Form rules (required fields, conditional logic, custom thank-you page, branding) are all UI-only.
- **Conditional Formatting** (cell-color rules based on cell values or row state) — UI-only.
- **Filter Views** (saved per-user filter definitions over a sheet) — UI-only.
- **Restrict to dropdown values only** (PICKLIST column validation toggle) — UI-only. Critical for `shared/picklist_sync.py` activation: the sync writes the option list, but the "reject free-text entries" enforcement toggle must be set manually per column. Without it, picklist sync still works but users can type values that aren't in the master DB (canonical-name drift).

**Impact on `shared/picklist_sync.py`:** the `Restrict to dropdown values only` toggle must be manually set on each downstream PICKLIST column at deployment time. Without it, the sync still works (options stay in sync) but the strict-mode validation that prevents users from typing vendor-name drift is absent. Documented in `docs/references/picklist_sync.md` activation checklist step 5.

**Impact on form-and-clone cascade:** every form requires manual UI setup. The cascade flow assumes operator builds forms in the UI as the final cutover step.

**Resolves if:** Smartsheet exposes any of these surfaces via API. Worth re-checking annually — Smartsheet's API surface expands slowly. No action item today; this entry exists so future operators / new customer forks know the manual-deployment-step list without rediscovering it.

**Urgency:** none. Operationally accepted; manual deployment steps documented per-customer.

Surfaced: Phase-0 architecture review 2026-05; referenced from `docs/references/picklist_sync.md` activation checklist.

### ITS_Active_Jobs column order cosmetically scrambled [OPEN 2026-06-05, low]

The 4 contact columns (Stakeholder Name, Stakeholder Email, Stakeholder Phone, Safety Reports Contact Email) were added one-at-a-time to ITS_Active_Jobs after the initial schema, causing them to interleave with Active/Notes and the system columns in the Smartsheet UI. Column order is not load-bearing — `shared/active_jobs.py` looks up columns by title, not position. Reorder in the Smartsheet UI if desired for operator readability.

**Tag:** `safety-portal`, `cosmetic`, `smartsheet-ui`.

**Effort:** ~5 minutes (UI drag-to-reorder).

**Revisit when:** convenience; not a blocker.

Surfaced: 2026-06-05 Safety Portal Phase 3 session (PR #160).

---

## Cloudflare

### Cloudflare D1 `/query` intermittently 403s (code 7403) then succeeds on retry [OPEN 2026-08-10]

Observed 3× this session against account `a1d033090d474174c43fd3d0e6f7a0ab` — a `/query` call fails 403
`code 7403`, then an immediate identical retry succeeds. `wrangler d1 list` against the same account is
unaffected. Not yet diagnosed (token-scope propagation delay vs. a genuine rate/consistency edge on
Cloudflare's side). No code currently retries this class in the D1-facing paths that would hit it live.
**Trigger:** if it starts producing operator-visible failures rather than only appearing in interactive/CLI
use. **Tag:** `cloudflare`, `d1`, `host-migration`.

---

### Local `wrangler d1` commands fail on this dev host (`_cf_ALARM has 3 columns but 2 values`) — remote/deploy confirmed unaffected [OPEN 2026-08-11]

Any **local** D1 command (`wrangler d1 migrations apply --local`, `wrangler d1 execute --local`)
dies with `Fatal uncaught kj::Exception: … table _cf_ALARM has 3 columns but 2 values were
supplied: SQLITE_ERROR` — a `workerd` local-runtime fault (first seen wrangler 4.105.0), not a
project bug. It survives wiping `.wrangler/state` and reappears after `npm ci` in a fresh
worktree. `--remote` and `npm run deploy` are confirmed NOT affected (neither starts the local
runtime) — do not chase this as a deploy blocker or recommend a wrangler upgrade on account of it.

**Fix:** no project-side fix available (upstream `workerd`/wrangler issue). Two working
substitutes when a migration needs local validation: (1) stdlib `sqlite3`, `executescript`-ing
every prior migration file in order then running the one under test; (2) the vitest worker suite,
which applies every migration through real `workerd` + D1 already. See auto-memory
`reference_wrangler-local-d1-cf-alarm-fault.md`.

**Tag:** `tooling`, `wrangler`, `cloudflare`, `low`.

**Revisit when:** a wrangler upgrade is taken, to check whether the upstream fault is resolved.

Surfaced: 2026-08-11 session close.

---

## Exchange Online / PowerShell

### PowerShell `Get-ApplicationAccessPolicy -Identity <friendly-name>` directory lookup fails [OPEN 2026-05-20]

`Get-ApplicationAccessPolicy -Identity <friendly-name>` fails with a directory-object-not-found error in Exchange Online PowerShell, even when the policy exists and is valid.

**Workaround:** call the bare cmdlet (no `-Identity`) and filter the result set client-side. Pattern: `Get-ApplicationAccessPolicy | Where-Object { $_.Description -match '<keyword>' }` or pipe to `Select` and pattern-match the returned rows.

Captured 2026-05-20 during M365 sandbox re-verification while validating the `ITS Scoped Mailboxes` policy for R2 Watchdog Check F. The bare-cmdlet form returned a valid record with `IsValid: True` despite the friendly-name lookup failing seconds earlier on the same policy.

---

## GitHub Actions

### `gh pr update-branch` is not a reliable CI trigger [OPEN 2026-08-03]

Observed on PR #722 (2026-08-06): `gh pr update-branch` produced a new head SHA, but GitHub fired
**only** the CodeQL default-setup workflow against it. The in-repo `ci` workflow — whose `test`,
`portal` and `secrets` jobs are all REQUIRED by branch protection — never triggered, so the PR sat
`BLOCKED` with no runs to wait on. `gh pr close` + `gh pr reopen` did not wake it either. An empty
`git commit --allow-empty` + push did.

This is a NEW variant of the existing CI-ghost class (`ci-ghost-check-watch-hang` memory): there the
symptom was a check-run stuck `IN_PROGRESS` on a CLEAN PR; here the runs simply never existed.
Distinguish them by querying check-runs on the PR's exact head SHA
(`gh api repos/<r>/commits/<sha>/check-runs`) rather than the branch — a branch-scoped
`gh run list` shows the PREVIOUS head's runs and looks reassuring.

**Trigger:** low priority; the workaround is one command. Worth folding into
`docs/operations/pr_merge_discipline.md` next time that file is touched. **Tag:** `ci`, `github`.

---

## Deliberate ITS design choices

### Smoke harness pattern divergence between dedupe smoke and Resend/Sentry smokes [OPEN 2026-05-20]

`scripts/smoke_test_alert_dedupe.py` uses the full `@its_error_log` decorator path so all three triple-fire legs fire (Smartsheet `log()` write + Resend + Sentry). `scripts/smoke_test_sentry.py` and `scripts/smoke_test_resend.py` call `shared.error_log._alert_critical` directly, which deliberately bypasses `log()` and therefore does NOT write to ITS_Errors.

The divergence is acceptable because the older two scripts validate narrower scopes (the Sentry leg, the Resend leg), and the alert-dedupe smoke validates the cross-leg integration. The trap is that the `_alert_critical`-direct pattern silently skips the Smartsheet leg — if a future smoke claims to exercise full triple-fire but uses that pattern, the ITS_Errors assertion will pass vacuously (zero rows match, zero rows expected by the harness).

**Action:** any new smoke that intends to verify all three legs MUST go through the `@its_error_log` decorator. Smoke that targets a single leg can keep the `_alert_critical`-direct pattern.

**Urgency:** low. No active failure; this entry is forward-protection for the next time someone writes a triple-fire smoke. Discovered post-PR-#42 merge when the operator's live run produced 0 ITS_Errors rows.

Surfaced: PR α (alert-dedupe-core) live verification, 2026-05-20.

### Phase 5 manual week-sheet additions [OPEN 2026-06-05]

Operator-decided edge case (2026-06-05): if a PM submits a safety doc directly (outside the portal) for a specific job-week, the operator adds a row + the safety doc directly to the per-job week sheet, fills the relevant cells; `intake.py` ignores the manually-added row and `weekly_generate.py` rolls it into the compiled packet like any other doc. This is by design — no automation needed for an occasional manual correction.

**Tag:** `safety-portal`, `operator-workflow`.

**Revisit when:** Phase 5 build. Low-urgency; operator-decided.

Surfaced: 2026-06-05 Safety Portal Phase 3 session (PR #160).

### `render_submission_pdf` is not byte-deterministic — pre-existing, deliberate, now explicitly documented (2026-07-23, PR #693)

The document-polish session's byte-determinism adversarial-review lens verified PO/RFQ/subcontract-package/
zip/quote-form renders are byte-identical across repeated in-process AND cross-process runs (different
`PYTHONHASHSEED`s) — `render_submission_pdf` (the safety/progress form-PDF renderer in `form_pdf.py`) was
NOT included in that determinism set and is not expected to be: it embeds a wall-clock "Filed at" style
timestamp in the rendered output by design, so byte-identity across two renders of the same submission is
not a meaningful property for it. This predates PR #693 and is not a regression the session introduced —
it is recorded here because the adversarial-review pass surfaced it as worth naming explicitly rather than
leaving it as an implicit assumption. **Trigger:** none — informational; revisit only if a future feature
(e.g. a render-diff/dedup tool) needs safety/progress PDFs to be byte-deterministic, at which point the
timestamp field would need to move out of the rendered bytes (e.g. into filename/metadata only).
**Tag:** `form_pdf`, `determinism`, `informational`, `low-severity`.
