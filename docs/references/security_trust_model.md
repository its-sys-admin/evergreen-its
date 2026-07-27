---
type: reference
date: 2026-07-14
status: active
workstream: null
tags: [documentation-corpus, tier-1]
---

# ITS Security & Trust Model

## Purpose

<!-- src: CLAUDE.md (## System-wide invariants) | verified 2026-07-14 -->
This is the operator-facing map of how ITS defends itself. It describes the two
non-negotiable security boundaries — the **External Send Gate** (nothing leaves the
building without a verified human approval) and **Adversarial Input Handling** (every
byte from outside the customer tenant is treated as untrusted data, never as
instructions) — plus the supporting controls: capability gating enforced in CI, the
macOS-Keychain secrets model with its write-only rotation ceremony, and the
PIN-gated operator dashboard. For each control this doc states the healthy signal an
operator should expect to see, so a normal day is distinguishable from a bad one.

<!-- src: safety_reports/photo_screen.py:23-31 | verified 2026-07-14 -->
The framing here is deliberately **defensive**: it explains what each control does
and how to confirm it is working, not how to defeat it. When a defense has a known
limit (for example, the PO PDF scanner cannot see inside compressed object streams),
that limit is stated plainly so an operator understands the real posture rather than
a false sense of coverage.

## Background — the two permanent invariants

<!-- src: CLAUDE.md (### Invariant 1 — External Send Gate) | verified 2026-07-14 -->
Two invariants, inherited from the planning layer (Foundation Mission v11), sit above
every workstream and are **permanent** — not time-boxed, not waivable:

| Invariant | What it guarantees | Where it is enforced |
|-----------|--------------------|----------------------|
| **1 — External Send Gate** | No external transmission without explicit human approval. Generation (AI) and sending (transmit) live in **separate processes**, so a successful prompt injection at the AI layer still cannot send. | `tests/test_capability_gating.py` at CI; the F22 approval gate at send time. |
| **2 — Adversarial Input Handling** | All content from outside the operating tenant is untrusted **data**, never trusted instructions. A six-layer defense assumes injection *may* succeed at the AI layer and caps the damage at "extracted data is wrong," never "data exfiltrated" or "action taken on an attacker's behalf." | `shared/quarantine.py`, `shared/untrusted_content.py`, `shared/anomaly_logger.py`, the attachment/photo screeners. |

<!-- src: CLAUDE.md (## System-wide invariants) — residual-risk paragraph | verified 2026-07-14 -->
The architecture openly assumes prompt injection is an unsolved research problem. The
design does not bet on preventing injection; it bets on making a successful injection
harmless, because the AI process holds no send capability and no external-action
capability (Op Stds §50 privileged-actuation gate governs the rare code-committing
daemons separately).

---

## 1 — The External Send Gate

### Two-process split

<!-- src: safety_reports/weekly_send.py:1-12 | verified 2026-07-14 -->
Every workstream that can produce output for **any external recipient — a customer,
vendor, or subcontractor** (Foundation Mission v11 Invariant 1) — is split into a
**generation script** (which may call the Anthropic API but has zero send capability)
and a **send script** (which transmits but has zero AI capability). For Safety Reports
the pair is `weekly_generate.py` (compile) and `weekly_send.py` (transmit); Purchase
Orders mirror it with `po_generate.py` / `po_send.py`; Progress Reporting with
`progress_weekly_generate.py` / `progress_send.py`; Subcontracts with
`subcontract_generate.py` / `subcontract_send.py`; and the RFQ lane with
`rfq_generate.py` / `rfq_send.py` — the last two transmit to subcontractors and
vendors respectively, which the older "customer-facing" phrasing excluded. The split is the structural
reason a prompt injection cannot cause a send: the transmitter is a different OS
process that never imported the model.

```
   UNTRUSTED INPUT                          GENERATION PROCESS (AI, no send)
  ┌────────────────┐   HMAC-verified pull  ┌──────────────────────────────┐
  │ portal / email │ ────────────────────> │  intake / weekly_generate    │
  │  submissions   │                       │  · wrap() untrusted content  │
  └────────────────┘                       │  · anomaly_logger.check()    │
                                           │  · renders PDF, files to Box │
                                           └───────────────┬──────────────┘
                                                           │ writes a review row
                                                           v
                                        ┌──────────────────────────────────┐
                                        │  <WS>_human_review Smartsheet     │
                                        │  row: Send Now / Approve for      │
                                        │  Scheduled Send  (HUMAN checks it) │
                                        └───────────────┬───────────────────┘
                                                        │ poller discovers it
                                                        v
   SEND PROCESS (transmit, no AI)  ┌───────────────────────────────────────┐
  ┌──────────────────────────────┐ │ weekly_send_poll (iterator/dispatcher)│
  │ weekly_send.send_one_row     │<│  · F22 verify_approval (fail-CLOSED)  │
  │  · recipients @ send time     │ │  · stamps verified approver           │
  │  · SENDING write-ahead marker │ └───────────────────────────────────────┘
  │  · graph_client.send_mail     │
  └──────────────────────────────┘
```

### MANUAL-mode approval row

<!-- src: safety_reports/weekly_send.py:22-40 (send_one_row pipeline) | verified 2026-07-14 -->
A compiled report is never sent by the act of compiling it. Generation writes a row
to the workstream's `*_human_review` sheet (Safety: `WSR_human_review`) with
`Send Status = PENDING` and an editable `Email Body`. A human reviewer opens the row,
edits the body if needed, and checks one of two approval boxes — **`Send Now`**
(immediate) or **`Approve for Scheduled Send`** (the Monday ≥ 07:00 Pacific batch).
Only then does the send poller consider the row a candidate.

<!-- src: safety_reports/send_poll_core.py:214-229 (_filter_dispatch_candidates) | verified 2026-07-14 -->
The send poller (`weekly_send_poll.py`, a launchd daemon on a 15-minute
`StartInterval`) is a pure **iterator + dispatcher** — it holds no send capability of
its own. Each cycle it reads the review sheet, keeps only rows whose approval box is
checked and whose `Send Status` is in the dispatch set `{PENDING, FAILED}`, and (for
`FAILED` rows) drops any that have already exhausted `MAX_SEND_RETRIES`.

### F22 — approval-attestation (`verify_approval`)

<!-- src: shared/approval_verification.py:1-47 | verified 2026-07-14 -->
A checked box is a *value*, and a value alone could be hand-edited or flipped by an
unauthorized account. The **F22** control (`shared/approval_verification.py`,
`verify_approval`) closes that gap: before any dispatch, it reads the approval cell's
Smartsheet **modification history** and confirms the current approved value was set
by an authorized human. The send process never trusts the column value alone again.

<!-- src: shared/approval_verification.py:17-27, 160-254 | verified 2026-07-14 -->
`verify_approval` is **fail-CLOSED and total** — it never raises; every path returns
an `ApprovalVerdict`, and any non-`verified` verdict means "do NOT send." This is the
deliberate opposite of the observability fail-open posture: a missed send is
recoverable, a send authorized by the wrong party is not. The reason a raising API
was rejected: a caller that forgot the `try/except` could let an exception slip past
the send decision. A verdict the caller must inspect cannot be silently bypassed.

<!-- src: shared/approval_verification.py:87-95, 51-66 | verified 2026-07-14 -->
Every failure folds into a `verified=False` verdict carrying a `VerdictReason` the
caller uses to pick alert severity:

| VerdictReason | Meaning | Operator signal |
|---------------|---------|-----------------|
| `AUTHORIZED` | Current approval set by an authorized actor. | Healthy — the send proceeds. |
| `UNAUTHORIZED_ACTOR` | Most-recent approver is not in the authorized set. | **Security-relevant → CRITICAL page.** |
| `EMPTY_ALLOWLIST` | Authorized set is empty (no workspace shares / read miss). | **CRITICAL page** (cutover/sharing failure). |
| `NOT_CURRENTLY_APPROVED` | Cell un-approved between filter and check (benign race). | WARN only, no page. |
| `NO_HISTORY` | Cell has no modification history. | Fail-closed, blocked. |
| `HISTORY_READ_FAILED` | History read/parse raised (typically transient infra). | ERROR, no page. |

<!-- src: safety_reports/send_poll_core.py:254-286 (_handle_unverified) | verified 2026-07-14 -->
A blocked row always writes a forensic `approval_unverified` row to `ITS_Errors`.
The operator is paged (the triple-fire CRITICAL path, dedupe-gated) only for the two
security-relevant reasons — `UNAUTHORIZED_ACTOR` and `EMPTY_ALLOWLIST`. One blocked
row never stops the others: the dispatch loop is per-row fenced.

### §46 — authorization by workspace share

<!-- src: shared/smartsheet_client.py:1512-1540 (list_workspace_share_emails) | verified 2026-07-14 -->
Who counts as an authorized approver is **not** a hand-maintained config list. Under
Op Stds §46, the authorized set *is* the membership of the workstream's Smartsheet
workspace share list. `list_workspace_share_emails(workspace_id)` returns the
lowercased emails the workspace is directly shared with; sharing the ITS — Safety
Portal (or ITS — Purchase Orders) workspace with a person **is** granting them
approval authority. Only individual (USER) shares carry an email — GROUP shares have
none and are excluded.

<!-- src: safety_reports/send_poll_core.py:156-167 (_load_authorized_approvers) | verified 2026-07-14 -->
The poller loads that set with **no `try/except`** — deliberately. Unlike a config
read (which fails open to a scheduling default), a failure reading the approver set
(circuit-open, auth, 500) MUST propagate → CRITICAL → the cycle aborts with zero
sends. An *empty* result is the legitimate fail-closed case (`EMPTY_ALLOWLIST` blocks
all sends), never "allow all."

<!-- src: shared/approval_verification.py:37-44, shared/sheet_ids.py:36-43 | verified 2026-07-14 -->
Because Smartsheet cell-history exposes only `{name, email}` (no stable user id),
identity is matched on **email, case-insensitively**. This makes the authorized set
sensitive to the sandbox → production cutover: production reviewers are different
email identities, so the production workspace must be re-shared with them at delivery
or every send silently fail-closes. This is a tracked cutover-checklist item, not a
code change.

### Double-send protection (write-ahead SENDING marker)

<!-- src: safety_reports/weekly_send.py:560-582 | verified 2026-07-14 -->
Just before the irreversible Graph send, `send_one_row` flips the row to
`Send Status = SENDING` as a **write-ahead intent marker**. `SENDING` is *not* in the
poller's dispatch set, so if the post-send SENT stamp later fails, the row is left in
`SENDING` and is never re-dispatched — the customer is not double-sent. If the SENDING
write itself fails, nothing has been sent yet, so the handler returns without sending
and the row retries next cycle. The design always fails toward *not* sending.

### HELD — operator-actionable refusals

<!-- src: safety_reports/weekly_send.py:107-124, 22-40 | verified 2026-07-14 -->
`send_one_row` refuses to transmit a half-formed packet. It marks the row **HELD**
(no send, no auto-retry, operator must act) on: empty/unknown recipient
(`held_no_recipient` / `held_missing_envelope`), a missing compiled PDF
(`held_missing_pdf` — recompile needed), or a packet beyond Graph's upload-session
ceiling (`held_oversized_packet` — reduce photos / split). Recipients are resolved at
**send time** from `ITS_Active_Jobs` via the row's Job ID (TO = the job's
safety-reports contact, CC = the job's CC 1–5), not from the review row's display
columns.

### Cross-workstream contamination guard

<!-- src: safety_reports/weekly_send.py:17-21, 400-444 | verified 2026-07-14 -->
Because the dispatch logic is shared across workstreams (parameterize-not-clone), the
send handler reads each row's `Workstream` tag before sending and **HARD-HELDs**
(`held_workstream_mismatch` + CRITICAL `weekly_send.workstream_mismatch`) any row
whose tag does not match the sender's expected value (`safety` for the safety
sender). A missing tag WARNs and proceeds (pre-backfill back-compatibility). A safety
send therefore can never accidentally transmit a progress or PO row through safety's
recipients.

---

## 2 — Adversarial-input defense (six layers, as implemented)

<!-- src: CLAUDE.md (### Invariant 2 — Adversarial Input Handling) | verified 2026-07-14 -->
Invariant 2 is realized as six layers. The load-bearing prevention is Layers 2–4 plus
the two-process Send Gate; **Layer 5 is a post-hoc tripwire, not a barrier** (see
below).

```
  external content
        │
        v
  ┌─────────────────────────────────────────────────────────────────┐
  │ L1  Sender allowlist + header-forgery detection  (quarantine.py) │
  │ L2  Untrusted-content XML tagging               (untrusted_...py) │  ← prevention
  │ L3  Capability gating (AI has no send/act)      (Invariant 1)     │  ← prevention
  │ L4  Structured-output enforcement (tool-use JSON schema)          │  ← prevention
  │ L5  Anomaly logging — POST-HOC TRIPWIRE, not a barrier (anomaly_) │  ← detection
  │ L6  Attachment / photo screening   (photo_screen / po_attach_...) │  ← prevention
  └─────────────────────────────────────────────────────────────────┘
```

### L1 — Sender allowlist + header-forgery detection

<!-- src: shared/quarantine.py:1-17, 55-81 | verified 2026-07-14 -->
`shared/quarantine.py` is the allowlist boundary for email-borne intake. Non-
allowlisted senders route to `ITS_Quarantine` — **no Anthropic call ever runs on
quarantined content** (defense in depth: nothing that failed the allowlist reaches
the model). `is_allowlisted(sender, allowlist)` matches an exact address or an
`@domain` pattern, case-insensitively. `log_quarantined_message(...)` writes the audit
row and **propagates** any Smartsheet failure to its caller — losing the audit record
of who was quarantined is itself a security-relevant incident, so the caller elevates
it to CRITICAL rather than swallowing it.

<!-- src: shared/quarantine.py:37-53 | verified 2026-07-14 -->
Disposition reasons are carried as a `QuarantineReason` enum
(`unknown_sender`, `sender_disabled`, `workstream_out_of_scope`,
`header_forgery_suspected`, `legacy_allowlist_miss`). Header-forgery detection
(SPF/DKIM/DMARC + Return-Path validation) precedes the allowlist lookup. Note the
quarantine workstream catch-all is `other`, **not** `global` (it differs from
`ITS_Review_Queue` — a common footgun).

### L2 — Untrusted-content tagging

<!-- src: shared/untrusted_content.py:1-27, 72-108 | verified 2026-07-14 -->
`shared/untrusted_content.py` wraps every piece of external content in
`<untrusted_content source="...">` tags and supplies the canonical system-prompt
boilerplate that tells the model: content inside those tags is **data to analyze**,
and any instructions inside them are to be ignored. Two neutralizations are
load-bearing and must not be "simplified away":

- <!-- src: shared/untrusted_content.py:101-107 | verified 2026-07-14 -->
  The `source` label is sanitized (quotes / angle brackets / backslashes stripped) so
  a hostile label cannot break out of the attribute context.
- <!-- src: shared/untrusted_content.py:16-21, 107 | verified 2026-07-14 -->
  Any literal `</untrusted_content>` inside the content is broken with a zero-width
  space so attacker-supplied text cannot emit a second closing tag and escape the
  trust boundary (tag-breakout injection). The output contains exactly one closing
  tag regardless of input.

The system boilerplate remains the primary defense; this in-module neutralization is
defense-in-depth for the one module whose entire job is adversarial-input handling.

### L3 — Capability gating

<!-- src: tests/test_capability_gating.py:1-19 | verified 2026-07-14 -->
The AI has no permission to send or take action. This is Invariant 1 restated as an
input-defense layer: even if injection succeeds, the compromised process holds no
send capability. Enforced in CI — see section 3.

### L4 — Structured-output enforcement

<!-- src: shared/anomaly_logger.py:15-18, 71-80 | verified 2026-07-14 -->
Anthropic tool-use forces every extraction into a JSON-schema-conforming response;
non-conforming output is rejected. Per-field `maximum` bounds in the schema are the
hard ceiling — Layer 5's numeric check backstops them if that ceiling is ever bypassed.

### L5 — Anomaly logging (a post-hoc tripwire, NOT a barrier)

<!-- src: shared/anomaly_logger.py:1-32, 44-48 | verified 2026-07-14 -->
`shared/anomaly_logger.check(extracted)` runs on every extraction output and returns
a list of human-readable anomaly descriptions; a non-empty list routes the item to
`ITS_Review_Queue` with `security_flag=True`. **It is critical to understand this is
detection, not prevention** (reframed by audit F13). It matches known-suspicious
patterns by exact substring / anchored field-name regex — trivially evaded by
paraphrase — so it raises a signal *after* the fact; it never *blocks* a successful
injection. Never rely on it as a barrier: the real prevention is Layers 2–4 plus the
Send Gate.

<!-- src: shared/anomaly_logger.py:49-80 | verified 2026-07-14 -->
What it looks for: injection-control field names an honest schema would never invent
(`recipient_override`, `send_to`, `external_address`, anchored `ignore_*` / `role_*` /
`system_*` control names — narrowed so legitimate `system_version` / `role_description`
no longer false-fire); field values over 2 KB; well-known injection phrases in any
string; and (F21) any int/float above `NUMERIC_ANOMALY_THRESHOLD` (1000) — an inflated
count such as a prompt-injected `99999` incident total. `bool` is excluded from the
numeric check so checkbox flags never trip it.

### L6 — Attachment & photo screening (§34)

<!-- src: safety_reports/photo_screen.py:1-37 | verified 2026-07-14 -->
Layer 6 is the byte-level trust boundary for files. It runs **on the Mac** (never on
the send-free Worker), is deterministic, send-free, and LLM-free, and classifies bytes
before they reach a PDF renderer or Box. There are two sibling screeners.

**Photo screening — `safety_reports/photo_screen.py`** (Safety Portal site photos):

<!-- src: safety_reports/photo_screen.py:58-72, 122-153, 184-222 | verified 2026-07-14 -->
| Layer | Check | Verdict on failure |
|-------|-------|--------------------|
| L1 | Magic-number (JPEG/PNG only) + decoded-size cap (`MAX_DECODED_BYTES` 400 000) + per-submission count cap (`MAX_PHOTOS_PER_SUBMISSION` 8). | `suspicious` |
| L2 | Pillow `verify()` + decompression-bomb pixel cap (`MAX_IMAGE_PIXELS` 24 000 000) + **forced re-encode** to a fresh baseline JPEG. | bomb → `malicious`; unreadable → `suspicious` |
| L3 | Optional ClamAV on the **original** bytes, config-gated `safety_reports.photo_screen.clamav_enabled` (default OFF). | signature → `malicious`; required-but-unavailable → `suspicious` |

<!-- src: safety_reports/photo_screen.py:10-16, 143-153 | verified 2026-07-14 -->
The **forced re-encode is the load-bearing sanitizer**: rebuilding the image from its
raw pixel buffer destroys any appended/polyglot payload past the codec end-marker and
strips all metadata (EXIF/GPS/ICC/XMP, including the JPEG comment marker Pillow would
otherwise re-emit). Only `clean` photos carry a re-encoded JPEG forward; `suspicious`
/ `malicious` bytes are refused, never filed. ClamAV scans the *original* bytes
because a re-encode would strip a payload before the scanner could see it.

<!-- src: po_materials/po_attach_screen.py:1-85 | verified 2026-07-14 -->
**Document screening — `po_materials/po_attach_screen.py`** (PO builder attachments):
the first real §34 document-class screener (PDF / OpenXML / image). L1 requires
magic ⇄ declared-MIME ⇄ extension to all agree (PDF, JPEG/PNG, `.docx`/`.xlsx` only)
plus a size cap and a filename gate that mirrors the Worker (rejects path separators,
control chars, and Unicode bidi/zero-width spoofing controls). L2 is format-aware:

<!-- src: po_materials/po_attach_screen.py:126-160, 240-301, 335-360 | verified 2026-07-14 -->
- **PDF** — a best-effort raw scan (after `#xx` hex-escape normalization) for
  active-content markers (`/JavaScript`, `/JS`, `/OpenAction`, `/AA`, `/Launch`,
  `/EmbeddedFile`, `/RichMedia`) → `suspicious`, refused-to-review. **Honest limit:**
  this is not a PDF parse — markers inside compressed object streams (`/ObjStm`, the
  modern default) are invisible to it (accepted limitation ATT-5; the operator posture
  is that PO attachments are a limited-blast-radius workflow, backed by optional
  ClamAV, not deep parsing).
- **OpenXML** — a bounded in-memory zip walk: entry-count and total-decompressed caps
  (zip bomb → `malicious`), a macro payload `vbaProject.bin` → `malicious`, nested
  executable extensions → `malicious`, external-template / OLE relationships →
  `suspicious`, container/extension mismatch → `suspicious`. **Honest limit:** DDE
  field codes and other content-level vectors are not inspected (ATT-6).
- **Images** — Pillow verify + bomb cap + a re-encode used only as *structural proof*
  (the original bytes are filed, since these are the operator's own specs/drawings and
  resolution fidelity wins). L3 is the same optional ClamAV pass, gated
  `po_materials.po_attach_screen.clamav_enabled` (default OFF).

<!-- src: po_materials/po_attach_screen.py:71-79 | verified 2026-07-14 -->
For both screeners a **malicious** verdict fires a CRITICAL that *names the uploading
account* and writes a `security_flag=True` Review-Queue row, refusing the file before
any filing; **suspicious** writes a Review-Queue row (never filed); **clean** proceeds.

---

## 3 — Capability gating and the CI test that blocks regressions

<!-- src: tests/test_capability_gating.py:1-19, 300-325 | verified 2026-07-14 -->
The External Send Gate is enforced structurally at CI time by
`tests/test_capability_gating.py`, which inspects each script's **static imports** by
AST. It carries two hand-maintained lists:

- **`GATED_SCRIPTS`** — generation scripts that must NOT import any send capability.
  Forbidden import substrings include `send_mail`, `resend`, `smtplib`, `email.mime`
  (and, for the deterministic compiles, `graph_client` and `anthropic` too — asserting
  they stay both send-free *and* LLM-free).
- **`SEND_SCRIPTS`** — send scripts that must NOT import any AI capability
  (`anthropic` / `anthropic_client` forbidden).

<!-- src: tests/test_capability_gating.py:567 (test_no_unallowlisted_network_imports), 596 (test_network_allowlist_has_no_stale_entries), 663 (test_every_convention_named_script_is_enrolled_or_exempt) | verified 2026-07-15 -->
Adding a new generation or send script to the correct list is "the entire enforcement
mechanism" for a new workstream — so three additional CI tests keep the lists honest:

| Test | What it guarantees |
|------|--------------------|
| `test_no_unallowlisted_network_imports` (F02) | No module on the untrusted-content surface (`shared/`, `safety_reports/`, `progress_reports/`, `field_ops/`, `po_materials/`, `operator_dashboard/`, `subcontracts/`) may import a network-egress or process-spawn library (`requests`, `httpx`, `socket`, `subprocess`, `boxsdk`, `anthropic`, `msal`, `pyclamd`, `importlib`, …) unless it is on `NETWORK_LIB_ALLOWLIST` with a one-line rationale. A future script that quietly `import requests` to exfiltrate fails here before it can ship. |
| `test_network_allowlist_has_no_stale_entries` | Every allowlisted file still exists and still imports a needle — no dead rubber-stamp entries. |
| `test_every_convention_named_script_is_enrolled_or_exempt` | Every module named `*_generate` / `*_send` / `*_poll` on the workstream surface is enrolled in `GATED_SCRIPTS` / `SEND_SCRIPTS` (or exempt with a reason) — closes the "forgot to enroll the new daemon" gap. |

<!-- src: tests/test_capability_gating.py:401-477 | verified 2026-07-14 -->
The F02 allowlist is the important defensive inversion: instead of asking "do these
named scripts avoid send capability," it asserts "no module that should never touch
the network *can* acquire that capability undetected." Each allowlisted file (e.g.
`shared/graph_client.py`, `shared/box_client.py`, `shared/anthropic_client.py`, the
photo/attachment screeners' `pyclamd`, the dashboard's `subprocess`/`importlib`
importers) carries an inline justification comment.

**Healthy signal.** All of `tests/test_capability_gating.py` is green on `main`. A red
result here is never a flaky test — it is a real Send-Gate or network-capability
regression and blocks the merge.

---

## 4 — Secrets model

<!-- src: shared/keychain.py:1-12, 82-127 | verified 2026-07-14 -->
Every ITS credential lives in the **macOS Keychain** — never in an env file, never
committed. Code reads a secret only through `shared.keychain.get_secret(name)`. A
missing entry raises `KeychainError`; a *locked* keychain raises the distinct
`KeychainLockedError` (common after a reboot before login) so a daemon fails with a
recognizable "unlock the keychain" signal rather than a misleading "not found." All
`security` CLI calls are bounded by a 10-second timeout so a daemon never hangs on a
locked-keychain prompt.

<!-- src: shared/keychain.py:129-181, 66-79 | verified 2026-07-14 -->
Writes go through `set_secret(name, value)`, which detects a controlling terminal and
splits the write form: a **daemon / headless** run feeds the value on stdin (never on
argv, so it never appears in `ps` / process-command capture — audit F04); a rare
**interactive** operator run passes the value on argv to avoid the `/dev/tty` prompt
that once corrupted the Box token. Cross-process writes are serialized with a
fail-open path lock, and a `CalledProcessError` scrubs any argv-borne value to `***`
before it can chain into a traceback.

### Keychain secret names (names only — never values)

<!-- src: operator_dashboard/act/registry.py:540-585 | verified 2026-07-24 -->
The rotatable-credential registry is a **fixed list** — the dashboard refuses to
rotate anything not on it. There is no free-form secret store.

| Keychain / Worker name | What it is | Rotation kind |
|------------------------|-----------|---------------|
| `ITS_SMARTSHEET_TOKEN` | Smartsheet API token | keychain (paste) |
| `ITS_RESEND_API_KEY` | Resend API key (operator alerts only) | keychain (paste) |
| `ITS_SENTRY_DSN` | Sentry DSN | keychain (paste) |
| `ITS_BOX_CLIENT_ID` | Box OAuth client id | keychain (paste) |
| `ITS_BOX_CLIENT_SECRET` | Box OAuth client secret | keychain (paste) |
| `ITS_MS_TENANT_ID` | Microsoft 365 tenant id | keychain (paste) |
| `ITS_MS_CLIENT_ID` | Microsoft Graph app (client) id | keychain (paste) |
| `ITS_MS_CLIENT_SECRET` | Microsoft Graph client secret — **expires** | keychain (paste) |
| `ITS_BOX_REFRESH_TOKEN` | Box OAuth refresh token | **box_guided** (never pasted) |
| `PORTAL_PO_API_TOKEN` (mirror `ITS_PORTAL_PO_TOKEN`) | Worker PO bearer | worker + mirror |
| `PORTAL_ESTIMATE_API_TOKEN` (mirror `ITS_PORTAL_ESTIMATE_TOKEN`) | Worker estimate bearer | worker + mirror |
| `PORTAL_RFQ_API_TOKEN` (mirror `ITS_PORTAL_RFQ_TOKEN`) | Worker RFQ bearer | worker + mirror |
| `PORTAL_CONFIG_API_TOKEN` (mirror `ITS_PORTAL_CONFIG_TOKEN`) | Worker config bearer | worker + mirror |
| `PORTAL_ADMIN_API_TOKEN` (mirror `ITS_PORTAL_ADMIN_TOKEN`) | Worker admin bearer | worker + mirror |

The three Microsoft Graph credentials are on the list because Graph is the only
transport for every external send: the client secret **expires** on an Entra-ID-set
lifetime, so an unnoticed expiry takes every send lane down at once. Rotating them
here is **Developer-Operator self-service** under the §44 v21.x rider (ratified
2026-07-14) — current-credential-gated self-rotation by the holder, which saves a
terminal round-trip. It does **not** make an expired Graph credential a Tier-2
repair: Class C is Developer-Operator-only, secrets/auth is a FIXED
high-capability class, and **a Successor-Operator who meets this escalates** rather
than rotating it. Record the expiry date at seed time and calendar the rotation.
All three re-seed together — a re-registered app changes tenant, client id, and
secret at once, and re-seeding two of the three leaves Graph fail-closed.

<!-- src: operator_dashboard/act/pin_change.py:1-8 (in-dashboard, current-PIN-gated PIN change) | verified 2026-07-15 -->
`ITS_ANTHROPIC_KEY` (the sole LLM key) is read at runtime but is **not** rotated through
the dashboard's secret-rotation registry — it is a paste-in-Keychain secret. The
`ITS_OPERATOR_PIN`, by contrast, **is** changeable in-dashboard, through a dedicated
Class-C **change-PIN** verb (`operator_dashboard/act/pin_change.py`) that is gated on the
*current* PIN rather than the write-only rotation registry (see section 5). ITS never
emits a secret *value* anywhere — logs, alerts, and audit rows name the key only.

### The write-only rotation ceremony

<!-- src: operator_dashboard/act/secret_rotate.py:1-13, 40-68 | verified 2026-07-14 -->
`operator_dashboard/act/secret_rotate.py` is **write-only and registry-bound**. Hard
rules, proven by tests: it never reads a secret back (there is no `get_secret` in the
module), never logs/echoes/persists a value except to its destination, and refuses any
key not in the registry. The audit row records `"<KEY> rotated by <op>"` — never the
value.

- <!-- src: operator_dashboard/act/secret_rotate.py:61-68 | verified 2026-07-14 -->
  **keychain kind** — write-through via `shared.keychain.set_secret` (the `-U`
  overwrite *is* the rotation).
- <!-- src: operator_dashboard/act/secret_rotate.py:71-107 | verified 2026-07-14 -->
  **worker kind** — `wrangler secret put <NAME>` with the value on **stdin, never
  argv**, cwd `safety_portal/`, then a **dual-write** of the byte-equal Keychain mirror
  from the same value. If the Worker secret sets but the mirror write fails, the
  outcome is recorded as a distinct `config_secret_mirror_desync` WARN (a
  half-completed rotation is never a silent fail-closed-daemon surprise).

### Quiesce-first for the Box refresh token

<!-- src: operator_dashboard/act/secret_rotate.py:10-12, 46-52 | verified 2026-07-14 -->
The Box refresh token is `box_guided` and is **never pasted** into the dashboard.
Because Box refresh tokens are single-consumer and rotate on **every** exchange, a
pasted value would immediately desync. The dashboard instead guides the operator
through the **quiesce → `setup_box_oauth.py` → smoke** ceremony: stop the Box-touching
daemons first, re-run the OAuth setup (which stores the freshly rotated token via the
`box_client._store_tokens` callback), then smoke-test before resuming. Missing that
persistence would let Box die within ~60 days, so it is locked by test.

---

## 5 — Operator dashboard auth tiers

<!-- src: operator_dashboard/auth.py:1-19 | verified 2026-07-14 -->
The operator dashboard is localhost-only (Tailscale-exposed), and its read-only
observability panels are ungated. Every **ACT** surface — anything that changes state —
sits behind `operator_dashboard/auth.py`. There are two ceremonies plus a CSRF layer.

```
   Class A  (config edits, gate flips, breaker clear)
   ───────────────────────────────────────────────
   PIN ──> SHA-256 both sides ──> hmac.compare_digest ──> apply
                                    (constant-time, no length oracle)

   Class B/C  (identity / trust / credentials / global brake)
   ─────────────────────────────────────────────────────────
   type exact confirmation phrase (the target's name)  ──┐
   AND re-enter PIN  ───────────────────────────────────┴─> apply
   (BOTH must match — fail-closed; shared throttle bucket)
```

### Class A — operator PIN

<!-- src: operator_dashboard/auth.py:5-9, 108-143 | verified 2026-07-14 -->
The PIN is read from Keychain `ITS_OPERATOR_PIN` and compared **constant-time**
(SHA-256 of each side, then `hmac.compare_digest` — no length oracle). It **fails
closed**: a missing or locked keychain *denies*. Brute force is throttled — 5 failed
attempts trigger a 60-second lockout and a CRITICAL page.

### Class B/C — elevated re-PIN + typed confirmation

<!-- src: operator_dashboard/auth.py:11-16, 146-157 | verified 2026-07-14 -->
Actions that change trust, identity, credentials, or the global brake require the
elevated ceremony: the operator **re-enters the PIN AND types an exact confirmation
phrase** (the target's name). Both must match — fail-closed; the typed confirm is
compared constant-time too. The elevated ceremony **shares the PIN throttle bucket**,
so the guess budget is 5 total across both ceremonies, not doubled per route.

### CSRF defense-in-depth

<!-- src: operator_dashboard/auth.py:159-181 | verified 2026-07-14 -->
An Origin/Referer allowlist runs on top of the PIN. A browser request with an
off-allowlist Origin is rejected; a request with *neither* header (a non-browser
script) is allowed through — because the PIN, not the header, is the real barrier.

**Healthy signal.** The dashboard ships **DARK** — fail-closed until `ITS_OPERATOR_PIN`
is provisioned. Before any Tailscale exposure the operator provisions a strong PIN;
until then every ACT route denies. A lockout page in `ITS_Errors`
(`config_pin_lockout`) means someone hit the 5-fail throttle — investigate.

---

## 6 — What is deliberately NOT editable, and why

<!-- src: operator_dashboard/act/registry.py:334-343, 401-423 | verified 2026-07-14 -->
The Class-A config editor is bound to a **fixed registry** — anything not in it is
read-only. Two categories are intentionally beyond *any* edit route, because being
able to turn them off would disable a security invariant:

| Setting | Why it is never editable |
|---------|--------------------------|
| `safety_reports.external_send_gate` | Editing it off would disable **Invariant 1**. It is Class E (read-only display only), asserted absent from the editable registry by a denylist test. |
| `safety_reports.authorized_approvers` (legacy row) | Shown for reference only. The **live** F22 approval authority is §46 workspace-share membership (`list_workspace_share_emails`), not this row — editing it would not change who can approve a send. |

<!-- src: operator_dashboard/act/registry.py:118-135, 287-330 | verified 2026-07-14 -->
Two further protections shape the editor:

- **`system.state`** (the global brake — ACTIVE / PAUSED / MAINTENANCE) is not a plain
  Class-A toggle: it is Class B, requiring the elevated ceremony, because pausing it
  halts scheduled daemons (high blast radius).
- **Send-poller gates** (`*.polling_enabled`) are `first_activation_gated`: a
  `false → true` edit is a potential dark → live first activation and is **routed to
  the escalate path (D1-3), not applied** — their Descriptions often carry a documented
  go-live precondition. A `true → false` *pause* is always a plain Class-A apply. This
  encodes the reflex "read a gate's Description before flipping it" as a control.

<!-- src: operator_dashboard/act/registry.py:255-330 | verified 2026-07-14 -->
Identity, trust, and endpoint settings (`*.from_mailbox`, `*.intake.mailbox`,
`intake.allowed_senders`, `reviewer_chain`, `portal.worker_base_url`) all sit in the
Class-B elevated tier — the same weight as a credential change. `*.poll_interval_seconds`
is install-time only (no hot reload) and is deliberately kept out of the editor
entirely.

---

## Edge cases & limitations (operator-facing)

<!-- src: shared/approval_verification.py:37-44 | verified 2026-07-14 -->
- **F22 is email-identity-bound.** At the sandbox → production cutover the production
  workspace must be re-shared with the real reviewers, or every send fail-closes with
  `EMPTY_ALLOWLIST` / `UNAUTHORIZED_ACTOR`. This is expected behavior, not a bug — it
  is the gate refusing to trust an unshared identity.
- <!-- src: po_materials/po_attach_screen.py:126-160 | verified 2026-07-14 -->
  **The PDF scanner is best-effort, by design.** Active-content markers hidden in
  compressed object streams (`/ObjStm`) are invisible to it (ATT-5); DDE field codes in
  OpenXML are not inspected (ATT-6). The accepted posture is a limited-blast-radius
  workflow plus optional ClamAV, not deep parsing.
- <!-- src: po_materials/po_attach_screen.py:304-333 | verified 2026-07-14 -->
  **PO image attachments are filed as originals.** Unlike the customer-facing photo
  path (whose re-encode *is* the sanitizer), the PO path keeps original bytes for
  resolution fidelity, so image polyglots and EXIF are not neutralized there —
  knowingly accepted for the operator's own specs/drawings.
- <!-- src: shared/anomaly_logger.py:44-48 | verified 2026-07-14 -->
  **Layer 5 is evadable by paraphrase.** Treat a `security_flag=True` Review-Queue row
  as a signal to look, never as proof that everything else is clean. Prevention lives
  in Layers 2–4 and the Send Gate.
- <!-- src: shared/keychain.py:44-48, 227-243 | verified 2026-07-14 -->
  **Keychain writes are fail-open on the lock.** A lost path lock writes anyway — a
  missed secret rotation is judged worse than a lost lock. This is a deliberate posture
  choice, distinct from the fail-closed send gate.

## Related docs

- `system_architecture.md` — the two-layer (planning / execution) model and the
  process topology the Send Gate depends on.
- `daemon_reference.md` — the launchd daemons named here (`weekly_send_poll`,
  `portal_poll`, `po_poll`) and their intervals / gates.
- `data_model_reference.md` — the `*_human_review` sheets, `ITS_Quarantine`,
  `ITS_Review_Queue`, `ITS_Errors`, and `ITS_Active_Jobs` referenced throughout.
- `integration_reference.md` — Smartsheet / Box / Graph / Anthropic client wrappers
  and the F02 network allowlist.
- `escalation_matrix.md` — CRITICAL page paths and the successor-operator (Tier-2)
  boundary for security-relevant faults.
- `glossary.md` — F02 / F13 / F22 / §34 / §46 / §50 and the Invariant vocabulary.
- `documentation_index.md` — the Tier-1 corpus index.
