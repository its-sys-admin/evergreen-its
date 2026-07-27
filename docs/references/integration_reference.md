---
type: reference
date: 2026-07-14
status: active
workstream: null
tags: [documentation-corpus, tier-1]
---

# ITS Integration Reference

## Purpose

<!-- src: shared/graph_client.py:1-35 | verified 2026-07-14 -->
ITS talks to eight external services. This reference gives the operator one section
per integration: the **auth model**, the **Keychain secret names** and **`ITS_Config`
keys** each one reads (names only — never values), the **known constraints** that have
bitten us, the **failure signatures** you will see in `ITS_Errors` or a daemon log, and
the **health/verify** command or watchdog check that tells you the integration is alive.
Every fact here was read out of the live client code this session; the invisible source
comments (visible in git, stripped from the PDF) cite the exact file and line.

<!-- src: shared/keychain.py:1-12 | verified 2026-07-14 -->
The organizing principle: **every ITS credential lives in the macOS Keychain**, read
through `shared/keychain.py`, never in an env file and never committed. When this doc
says "secret `ITS_FOO`" it means a Keychain generic-password entry named `ITS_FOO` on
the execution host. Rotating one is a **ceremony** (re-seed the Keychain entry), never a
value you paste into a config file.

## How to read this doc

<!-- src: shared/graph_client.py:76-108 | verified 2026-07-14 -->
Each `shared/*_client.py` wrapper follows the same shape, so learn it once: a lazy
singleton built from Keychain on first use; a **typed exception hierarchy** (a base
`*Error` plus HTTP-status subclasses) so business code never catches a raw SDK
exception; and thin operation helpers. The wrappers **never swallow** a failure — they
translate it to a typed exception and let the caller decide to log, quarantine, retry,
or surface. That is the "never silent" invariant in code form.

<!-- src: shared/graph_client.py:110-141 | verified 2026-07-14 -->
Every wrapper also **bounds its network calls with a timeout** (connect + read),
because an SDK with no default timeout can hang a launchd daemon indefinitely — the
class of bug that once hung an intake cycle ~88 minutes while holding a lock and
starving every later scheduled run. A hang is translated to a distinct timeout
exception so it is grep-distinguishable from a rate-limit or auth failure.

```
                        ┌─────────────────────────────────────────┐
                        │   macOS Keychain  (all secrets, names)   │
                        │  ITS_MS_* · ITS_BOX_* · ITS_SMARTSHEET_* │
                        │  ITS_RESEND_API_KEY · ITS_SENTRY_DSN     │
                        │  ITS_ANTHROPIC_KEY · ITS_PORTAL_*        │
                        └───────────────────┬─────────────────────┘
                                            │ get_secret()
             ┌──────────────────────────────┼──────────────────────────────┐
             │                              │                              │
      ┌──────▼──────┐               ┌───────▼───────┐              ┌────────▼───────┐
      │  shared/    │               │   shared/     │              │   shared/      │
      │ graph_client│               │smartsheet_/   │              │ box_client     │
      │  (M365)     │               │ resend_/sentry│              │ (OAuth user)   │
      └──────┬──────┘               └───────┬───────┘              └────────┬───────┘
             │ Mail send/read               │ SoR + alerts                  │ documents
      ┌──────▼──────┐               ┌───────▼───────┐              ┌────────▼───────┐
      │ Microsoft   │               │  Smartsheet   │              │     Box        │
      │ Graph v1.0  │               │  Resend/Sentry│              │  Enterprise    │
      └─────────────┘               └───────────────┘              └────────────────┘

   Cloudflare Worker (its-safety-portal) ── send-free D1 queue ──▶ portal_poll (Mac)
      HMAC-signs every submission                      pulls /api/internal/pending,
      never sends externally                           verifies HMAC, files via intake

   scripts/watchdog.py ──GET──▶ external heartbeat monitor (Healthchecks.io / audit F16)
         daily 07:00, one ping — skipped while system.heartbeat_url is the seeded placeholder
      Tailscale-only network posture — nothing ITS-owned is exposed to the public net
```

## Cross-cutting facts

### Keychain access

<!-- src: shared/keychain.py:82-127 | verified 2026-07-14 -->
`keychain.get_secret(name)` shells out to the macOS `security` CLI with a 10-second
timeout (`KEYCHAIN_CLI_TIMEOUT`). It raises `KeychainLockedError` (a subclass of
`KeychainError`) when `security` reports the keychain is **locked** — common after a
reboot before the login keychain is unlocked — so a daemon fails loud with a
recognizable signal rather than reporting a misleading "entry not found." The fix is
`security unlock-keychain`, not a code change.

<!-- src: shared/keychain.py:129-244 | verified 2026-07-14 -->
`keychain.set_secret(name, value)` is used only by flows that **rotate** a secret
programmatically (notably the Box refresh token). It detects a controlling TTY and
splits the write form: a headless daemon feeds the value on stdin (never on `argv`,
so it never lands in `ps` / EDR capture); a rare interactive operator run passes the
value on `argv` to dodge the `/dev/tty` prompt-trap that has corrupted the Box token
twice. Writes are serialized across processes by a fail-open sidecar lock
(`~/its/state/keychain_write.lock`).

### Anthropic API (the one LLM consumer)

<!-- src: shared/anthropic_client.py:22-35 | verified 2026-07-14 -->
`shared/anthropic_client.py` reads `ITS_ANTHROPIC_KEY` from Keychain and constructs an
`Anthropic()` client. Default model is `claude-sonnet-4-6` (`DEFAULT_MODEL`); a Haiku
model constant (`CLASSIFIER_MODEL = claude-haiku-4-5-20251001`) and an Opus constant
(`DEEP_REASONING_MODEL = claude-opus-4-7`) are defined for future high-volume or
deep-reasoning callers.

<!-- src: CLAUDE.md (What's stubbed vs. real — anthropic_client row) | verified 2026-07-14 -->
Per the current-state table, the **sole live inference call in the system** is
`safety_reports/intake.py`. It is the one place adversarial-input wrapping
(`shared/untrusted_content.py`) and post-hoc anomaly logging apply. The Anthropic key
is a Keychain secret like any other; this doc treats Anthropic as an internal reasoning
dependency rather than a data-integration surface, so it has no dedicated section below.

---

## Microsoft Graph / Microsoft 365

<!-- src: shared/graph_client.py:1-49 | verified 2026-07-14 -->
`shared/graph_client.py` is the Mail boundary to Microsoft 365 — the read side (inbox
listing, message + attachment fetch) and the **send** side (`send_mail`,
`send_mail_large_attachment`). It targets Graph v1.0 (`GRAPH_BASE =
https://graph.microsoft.com/v1.0`).

### Auth model

<!-- src: shared/graph_client.py:175-221 | verified 2026-07-14 -->
**MSAL client-credentials (app-only) flow** against an Entra ID app registration.
`_get_token()` builds a `msal.ConfidentialClientApplication` and calls
`acquire_token_for_client` with the `.default` scope. Tokens are cached in-memory at
module level (`expires_in` defaults to 3600s) and re-acquired when within
`TOKEN_REFRESH_MARGIN_SECONDS` (600s) of expiry — a ~50-minute effective cache with a
10-minute skew margin. The token cache is per-process, which is correct for
launchd-launched single-process scripts.

### Secrets & config

| Item | Name | Notes |
|------|------|-------|
| Tenant | `ITS_MS_TENANT_ID` | Keychain — read in `_get_token()` |
| App/client | `ITS_MS_CLIENT_ID` | Keychain |
| Client secret | `ITS_MS_CLIENT_SECRET` | Keychain |

<!-- src: shared/graph_client.py:191-205 | verified 2026-07-14 -->
All three are read from Keychain per token acquisition; there is no `ITS_Config` key for
Graph auth. The sandbox tenant is `evergreenmirror.com`; the proven smoke path is
`scripts/smoke_test_graph.py`.

### Known constraints

<!-- src: shared/graph_client.py:143-168 | verified 2026-07-14 -->
Graph offers two attachment transports and ITS honors both by **size**: an inline
`/sendMail` for small files (`INLINE_ATTACHMENT_MAX_BYTES` = 3 MB documents Graph's
inline ceiling) and an **upload session** for large files. Above
`UPLOAD_SESSION_MAX_BYTES` (150 MB) Graph rejects the attachment outright — no transport
exists. The upload session PUTs bytes in `UPLOAD_CHUNK_SIZE` chunks (3.125 MiB, a
320-KiB multiple). Note: the *caller* (`safety_reports/weekly_send.py`) picks the switch
threshold and switches to the upload session **below** 3 MB (at 2.5 MB) to leave
headroom for base64 + envelope overhead.

<!-- src: shared/graph_client.py:262-312 | verified 2026-07-14 -->
Requests retry on **429/503 only**, up to `MAX_RETRIES` (3), honoring `Retry-After`
with exponential-backoff fallback. A **timeout does not consume retries** — a hung host
rarely un-hangs within a cycle, and retrying would re-create the lock-starvation the
timeout exists to prevent; the launchd interval + watchdog are the recovery net.

### Failure signatures

| Exception | HTTP | Meaning |
|-----------|------|---------|
| `GraphAuthError` | 401 / token | MSAL token acquisition failed or Graph returned 401 |
| `GraphPermissionError` | 403 | Typically an **Application Access Policy** denial for the sending mailbox |
| `GraphNotFoundError` | 404 | Mailbox / message / attachment / folder missing |
| `GraphRateLimitError` | 429 | Retry budget exhausted |
| `GraphTimeoutError` | — | Connect/read hang (distinct so a *hang* is grep-distinguishable) |
| `GraphAttachmentTooLargeError` | — | Attachment over the 150 MB upload-session ceiling — operator-actionable HELD, not a transient retry |

<!-- src: shared/graph_client.py:80-108, 245-259 | verified 2026-07-14 -->
All six are subclasses of `GraphError`, so a caller that only catches `GraphError`
still fails toward *not sending*. A `403` in a send path almost always means the app's
Application Access Policy does not cover the `from_mailbox`.

### External Send Gate role

<!-- src: shared/graph_client.py:15-21, 586-589 | verified 2026-07-14 -->
This module **exposes** the send capability; it does not gate it. The architectural gate
(Foundation Mission Invariant 1) is at the workflow level: **generation scripts must not
import `graph_client`**, enforced by `tests/test_capability_gating.py`. `send_mail` and
`send_mail_large_attachment` are the two external-send surfaces.

### Health / verify

<!-- src: scripts/smoke_test_graph.py (referenced graph_client.py:5) | verified 2026-07-14 -->
Run `scripts/smoke_test_graph.py` for the proven sandbox send path. There is no
dedicated watchdog check for Graph reachability; send failures surface through the
per-workstream send daemons (`weekly_send`, `progress_send`, `po_send`) as `FAILED`/`HELD`
rows and CRITICALs on auth failure or retry exhaustion.

---

## Box

> ### ⚠ WARNING — Box is a SINGLE-CONSUMER OAuth account
>
> <!-- src: shared/box_client.py:16-36 | verified 2026-07-14 -->
> Box authenticates as **one real Box user** over OAuth, and its **refresh token
> rotates on every single token exchange**. The old token is invalid the instant a new
> one is issued. Two failure modes each kill Box for the **whole system within ~60
> days**, and both are the #1 way to break ITS from a second host:
>
> 1. **A second consumer.** Run a second process (a second host, a stray script, a
>    parallel worktree daemon) that also exchanges the refresh token, and the two
>    consumers invalidate each other's tokens. There must be exactly **one** Box
>    consumer.
> 2. **Failing to persist the rotated token.** If ITS exchanges the token, gets a new
>    one, then crashes before writing it to Keychain, the next invocation reads the old
>    (now invalid) token and fails auth.
>
> <!-- src: shared/box_client.py:31-36 | verified 2026-07-14 -->
> A refresh token is valid **60 days from last use**. Steady-state daily workstreams
> exercise it well inside that window; a multi-day host outage erodes the margin
> **invisibly**. Recovery from an expired or invalidated token is **re-running
> `scripts/setup_box_oauth.py`** (a browser OAuth flow) — a Developer-Operator
> (Seth-only) secrets operation.

### Auth model

<!-- src: shared/box_client.py:1-14 | verified 2026-07-14 -->
**OAuth 2.0 Authorization Code Grant**, configured as a Box **User Authentication** app
(not JWT/server-auth). ITS pivoted to this on 2026-05-20 because JWT requires the paid
Box Platform add-on that Evergreen's Enterprise tier does not include. The tradeoff:
ITS authenticates as a real Box **user** (the operator account during sandbox; a
dedicated ITS user at Phase 1.5 cutover), so audit trail and file ownership attribute to
that user.

<!-- src: shared/box_client.py:161-241 | verified 2026-07-14 -->
`get_client()` builds a `boxsdk.OAuth2` with the `_store_tokens` callback wired in, so
every rotation persists. `_store_tokens` writes the new refresh token to Keychain
**synchronously** under a cross-process sidecar lock (`~/its/state/box_oauth_refresh.lock`).
The lock is **fail-open**: a lock-acquire timeout persists anyway and logs
`box_oauth_refresh_lock_timeout` WARN — because an un-persisted token guarantees a
60-day death, whereas a lost lock is merely a rare race window. The test suite asserts
persistence explicitly (`test_store_tokens_persists_refresh_token`).

### Secrets & config

| Item | Name | Notes |
|------|------|-------|
| Client ID | `ITS_BOX_CLIENT_ID` | Keychain — operator-seeded once, never rotates here |
| Client secret | `ITS_BOX_CLIENT_SECRET` | Keychain — rotated manually in the Box console |
| Refresh token | `ITS_BOX_REFRESH_TOKEN` | Keychain — written by `setup_box_oauth.py`, **rotated on every use** |

<!-- src: shared/box_client.py:68-69 (OAUTH_TOKEN_URL / OAUTH_AUTHORIZE_URL); shared/box_client.py:95-97 (marker) | verified 2026-07-15 -->
Endpoints: token exchange at `https://api.box.com/oauth2/token`, authorize at
`https://account.box.com/api/oauth2/authorize`. A **freshness marker** is written on
every successful persist at `~/its/state/box_oauth_last_refresh.json`
(`BOX_TOKEN_REFRESH_MARKER`), read by watchdog Check P.

### Known constraints

<!-- src: shared/box_client.py:73-79 | verified 2026-07-14 -->
`boxsdk` has **no default network timeout**; ITS mounts a `(connect=10s, read=30s)`
timeout (`BOX_NETWORK_TIMEOUT`) on the `AuthorizedSession` so a stalled Box call cannot
hang a daemon. Retries fire on **429/503** only, up to `MAX_RETRIES` (3), honoring
`Retry-After`.

<!-- src: shared/box_client.py:345-376 | verified 2026-07-14 -->
`upload_bytes` is deliberately **not** retried — a `BytesIO` stream is consumed on the
first attempt, so a naive retry would re-send from EOF and upload an empty file. The
portal path handles a `BoxConflictError` (409) itself; `upload_bytes_or_new_version`
instead uploads a new Box **version** on conflict, preserving history.

### Failure signatures

| Exception | HTTP | Meaning |
|-----------|------|---------|
| `BoxAuthError` | 401 / 403 | Token rejected or insufficient scope (**expired refresh token** is the classic cause) |
| `BoxNotFoundError` | 404 | File / folder / resource missing |
| `BoxConflictError` | 409 | Duplicate filename in the destination folder |
| `BoxRateLimitError` | 429 | Retry budget exhausted |

<!-- src: shared/box_client.py:103-121, 222-226 | verified 2026-07-14 -->
All subclass `BoxError`. A `BoxAuthError` whose message mentions `setup_box_oauth.py`
means the credentials could not be read or the initial exchange was rejected — usually
the refresh token expired after >60 days idle, or was revoked from the Box console.

### Health / verify — watchdog Check P

<!-- src: scripts/watchdog.py:1514-1575 | verified 2026-07-14 -->
**Check P** reads the freshness marker and escalates *ahead* of expiry:
**WARN at 50 days idle** (`BOX_TOKEN_FRESHNESS_WARN_DAYS`, a 10-day buffer) and
**CRITICAL at 58 days** (`BOX_TOKEN_FRESHNESS_CRITICAL_DAYS`, a 2-day buffer before the
60-day death). A **missing marker** WARNs ("freshness unknown") — expected briefly right
after enabling the check, but a persistent absence means Box has never authed on this
host. The successor-remediation runbook is `docs/runbooks/box_token_freshness.md`:
re-seeding the token is a FIXED high-capability-class (secrets/auth) operation that
**always escalates to Seth**; the one Tier-2 action is restarting a merely-stopped
Box-writing daemon so the next exchange re-stamps the marker.

---

## Smartsheet API

<!-- src: shared/smartsheet_client.py:1-33 | verified 2026-07-14 -->
`shared/smartsheet_client.py` wraps the `smartsheet-python-sdk` so callers work in
**column-title** terms instead of column IDs, and so SDK exceptions never leak into
business code. Smartsheet is the **structured System of Record** for ITS-owned
operational sheets (`ITS_Config`, `ITS_Errors`, `ITS_Review_Queue`,
`ITS_Daemon_Health`, the `*_Log` / `*_human_review` sheets, `ITS_Active_Jobs*`, etc.).

### Auth model

<!-- src: shared/smartsheet_client.py:143-161 | verified 2026-07-14 -->
A single **API access token** (`ITS_SMARTSHEET_TOKEN` from Keychain) constructs the SDK
client with `errors_as_exceptions=True`, so non-2xx responses surface as SDK exceptions
that `_translate` maps to the typed hierarchy. A default-timeout adapter
(`SDK_NETWORK_TIMEOUT` = 30s) is mounted on the SDK's `requests` session because the SDK
itself has no default timeout.

### Secrets & config

| Item | Name | Notes |
|------|------|-------|
| API token | `ITS_SMARTSHEET_TOKEN` | Keychain — sole Smartsheet credential |
| Circuit breaker | `circuit_breaker.enabled` / `.failure_threshold` / `.cooldown_seconds` | `ITS_Config`, Workstream `global` |

<!-- src: shared/smartsheet_client.py:210-263, shared/defaults.py:62-64 | verified 2026-07-14 -->
The **F08 circuit breaker** wraps every network-issuing method: reads *and* writes count
toward tripping, but `401/403/404` are ignored (deterministic/routine — they must
surface as themselves, not a degraded-service signal). Config resolves from `ITS_Config`
under `circuit_breaker.bypass()` (so an OPEN breaker can't block the read of its own
kill flag), falling back to `defaults.py`: enabled `True`, threshold `5`, cooldown
`300s`. `get_setting`/`get_settings_with_prefix` are deliberately **undecorated** — they
delegate to the guarded `get_rows`, so guarding them too would double-count a failure.

### The column-title cache

<!-- src: shared/smartsheet_client.py:11-19, 277-293 | verified 2026-07-14 -->
Title→column-ID is cached per-sheet at module level, with a **refresh-once-on-miss**:
an unknown title triggers one column refetch (recovers an *added* column) but a
**rename is NOT recovered** — callers using the old title keep raising `KeyError`. That
is deliberate: silently writing into the wrong column is far worse than fast-failing.
Long-lived processes surviving a rename must restart or call
`invalidate_column_cache()`.

### Known constraints (Smartsheet platform + SDK gotchas)

| Constraint | Symptom / errorCode | Workaround |
|------------|---------------------|------------|
| **DATETIME needs a system column type** | user-defined `DATETIME` rejected — HTTP 500 / `errorCode 4000` | Use `ABSTRACT_DATETIME` (the UI "Date/Time" type) — creatable/retypable via `update_column` |
| **`ABSTRACT_DATETIME` value format** | offset or `Z` suffix rejected — `errorCode 5536` | Write a **naive** `YYYY-MM-DDTHH:MM:SS` (naive Pacific wall-clock, operator preference) |
| **AUTO_NUMBER rejected at sheet creation** | `type:AUTO_NUMBER` rejected — `errorCode 1008` | UI-only column add on the live sheet; code reads it once it exists |
| **Column FORMAT via dict constructor silently dropped** | `Column({"format": ...})` returns 200 but stays unformatted | Set `column.format = "..."` via the model **attribute** (width works either way) |
| **Sheet name > 50 chars** | HTTP 400 / `errorCode 1041` | Truncate at the composition site; raises `SmartsheetValidationError` (permanent, routes to Review Queue) |
| **`delete_rows` batch cap** | — | Smartsheet caps at **450 IDs** per call |

<!-- src: docs/tech_debt.md:698-712, 629-637 | verified 2026-07-14 -->
The DATETIME and AUTO_NUMBER restrictions are **permanent platform constraints**; the
mitigation for missing datetimes is Smartsheet's intrinsic row-level `created_at` /
`modified_at` attributes (full datetimes, queryable) for programmatic precision, with
the in-sheet DATE columns serving human readability.

<!-- src: docs/tech_debt.md:633 | verified 2026-07-14 -->
Column FORMAT descriptors index the account palette from `GET /2.0/serverinfo`
(`.formats.color`); positions are 2=bold, 8=textColor, 9=backgroundColor, 16=dateFormat.
`apply_column_styles` already uses the attribute path.

<!-- src: shared/smartsheet_client.py:1087-1202 | verified 2026-07-14 -->
Several helpers (`find_sheet_by_name_in_folder`, `find_folder_by_name_in_folder`,
`count_workspace_sheets`) use **direct REST** (`GET /2.0/folders/{id}`,
`GET /2.0/workspaces/{id}?loadAll=true`) rather than the SDK, because
`Folders.get_folder()` is deprecated upstream **and** returns stale data within a single
SDK client session — a sheet created via the SDK does not appear in a subsequent
`get_folder()` from the same client, while direct REST sees it immediately.

### Failure signatures

| Exception | HTTP | Meaning |
|-----------|------|---------|
| `SmartsheetAuthError` | 401 | Token rejected |
| `SmartsheetPermissionError` | 403 | Access denied for this sheet/resource |
| `SmartsheetNotFoundError` | 404 | Sheet / row / column / config setting missing (the *expected* "row not yet seeded" case for `get_setting`) |
| `SmartsheetRateLimitError` | 429 | SDK retry budget exhausted |
| `SmartsheetValidationError` | 400 | **Permanent** (`shouldRetry:false`) — e.g. the 50-char sheet-name cap; lets the portal drain route to Review Queue instead of looping |
| `SmartsheetCircuitOpenError` | — | Breaker OPEN, short-circuiting a sustained-degraded API |
| `SmartsheetWriteCapabilityError` | — | Token can READ but not WRITE (raised by the B2 probe) |

<!-- src: shared/smartsheet_client.py:53-111 | verified 2026-07-14 -->
All subclass `SmartsheetError`. The SDK emits the full response body at ERROR for every
non-2xx on the `smartsheet.smartsheet` logger; ITS **suppresses that for 404 only**
(the routine "row not seeded" case), keeping 401/403/429/500 visible on stderr.

### Health / verify — watchdog Check L

<!-- src: scripts/watchdog.py:1263-1306 | verified 2026-07-14 -->
**Check L (B2)** verifies the token can **WRITE**, not just read: it creates and deletes
a throwaway sheet named `_its_write_probe_*` each daily run. A read-only or mis-scoped
token (e.g. after a botched rotation) passes every read and only fails at the first real
daemon write — a silent mid-cycle 401. This probe turns that into a loud daily
`SmartsheetWriteCapabilityError` → CRITICAL. A Smartsheet **outage**
(`SmartsheetCircuitOpenError`) is INFO-skipped (not a token verdict); any other transient
is WARN-inconclusive.

---

## Resend

<!-- src: shared/resend_client.py:1-33 | verified 2026-07-14 -->
`shared/resend_client.py` is the **out-of-band operator-alert** email path — the third
leg of the triple-fire CRITICAL alert (Sentry + Smartsheet `ITS_Errors` + Resend). It
covers the case where an M365 outage would otherwise suppress its own outage alert. It
is **NOT for customer email** — customer email goes through `graph_client.send_mail`
under Invariant 1.

### Auth model

<!-- src: shared/resend_client.py:43, 78-89, 128-134 | verified 2026-07-14 -->
Resend's REST API has no SDK client object — auth is a **per-request Bearer header**.
`get_client()` loads and caches the API key from Keychain (`ITS_RESEND_API_KEY`); base
URL is `https://api.resend.com`.

### Secrets & config

| Item | Name | Notes |
|------|------|-------|
| API key | `ITS_RESEND_API_KEY` | Keychain — read directly by `get_client()` |
| Keychain-name indirection | `system.resend_api_keychain_key` | `ITS_Config` row (default `ITS_RESEND_API_KEY`) documented in the module header |
| Recipient | `system.operator_email` | `ITS_Config`, Workstream `global` — default `to` for an alert |
| Recipient fallback | `defaults.OPERATOR_EMAIL_FALLBACK` | build-time fallback when the config read is unavailable |

<!-- src: shared/resend_client.py:162-203 | verified 2026-07-14 -->
`send_alert(subject, body, to=None)` resolves `to` from `system.operator_email` at send
time, falling back to `defaults.OPERATOR_EMAIL_FALLBACK` (the ITS_Config read is a
circuit-breaker-guarded Smartsheet call, so it can short-circuit during exactly the
outage the page must reach the operator about; Resend is plain HTTP, unaffected). Body
is plain text only.

### Known constraints

<!-- src: shared/resend_client.py:44-56 | verified 2026-07-14 -->
Resend requires a **verified sender domain**. `DEFAULT_FROM` is Resend's sandbox
`onboarding@resend.dev` (pre-verified on every account, accepts any recipient) — the
right address for sandbox/smoke testing. It is swapped to the operator's verified Resend
domain at Phase 1.5 cutover; that constant is the only touchpoint.

<!-- src: shared/resend_client.py:45-49, 128-155 | verified 2026-07-14 -->
The alert path **fails fast**: `(connect=10s, read=30s)` timeout, and a network failure
is translated to `ResendError` and **not retried** (retry only on 429/503) — a
hung/unreachable host must not amplify into 3× the wait on the alert path; the durable
file + `ITS_Errors` legs of the triple-fire still land.

### Failure signatures

| Exception | HTTP | Meaning |
|-----------|------|---------|
| `ResendAuthError` | 401 / 403 | API key invalid/missing, or unauthorized for the sender domain |
| `ResendNotFoundError` | 404 | Endpoint / resource / sender not found |
| `ResendRateLimitError` | 429 | Retry budget exhausted |

<!-- src: shared/resend_client.py:59-73 | verified 2026-07-14 -->
All subclass `ResendError`. A sender-verification error surfaces as a clear
`ResendAuthError` until the operator both seeds the key and verifies the sender domain.

### Health / verify

<!-- src: shared/resend_client.py:5 | verified 2026-07-14 -->
Smoke path: `scripts/smoke_test_resend.py`. Runtime health is observed indirectly —
if Resend is the only failing leg of a triple-fire, the Sentry and `ITS_Errors` legs
still record the CRITICAL, and watchdog Check G sweeps the alert-dedupe summary.

---

## Sentry

<!-- src: shared/sentry_client.py:1-34 | verified 2026-07-14 -->
`shared/sentry_client.py` is the **second leg** of the triple-fire CRITICAL path
(Smartsheet `ITS_Errors` + Resend operator email + Sentry). Its job is forensic detail:
full traceback, environment, tags, and breadcrumbs in a web dashboard for when the
operator sits down to triage. The other legs answer "wake the operator up" (Resend) and
"durable log of every CRITICAL" (Smartsheet).

### Auth model

<!-- src: shared/sentry_client.py:61-95 | verified 2026-07-14 -->
Sentry's SDK is **process-globally configured** — there is no per-call client object.
`get_client()` runs `sentry_sdk.init` exactly once per process, reading the **DSN** from
Keychain (`ITS_SENTRY_DSN`).

### Secrets & config

| Item | Name | Notes |
|------|------|-------|
| DSN | `ITS_SENTRY_DSN` | Keychain — read by `get_client()` on first init |
| Keychain-name indirection | `system.sentry_dsn_keychain_key` | `ITS_Config` row (default `ITS_SENTRY_DSN`) documented in the module header |

<!-- src: shared/sentry_client.py:42, 82-90 | verified 2026-07-14 -->
Init settings are fixed in code: `environment="sandbox"`, `traces_sample_rate=0.0`
(**performance monitoring off** — this client exists for CRITICAL exception capture
only), and `send_default_pii=False` (explicit intent — no user IPs/cookies; for a
single-operator local system there is no PII to send anyway).

### Behavior

<!-- src: shared/sentry_client.py:97-140 | verified 2026-07-14 -->
`capture_exception(script, message, exc_info, correlation_id)` sends one event at
`level="fatal"` with tags `script`, `severity=CRITICAL`, `source=its-error-log`, and
`correlation_id` (when provided). The **correlation ID is shared across all three
triple-fire legs**, so the operator can grep Sentry / `ITS_Errors` / the Resend inbox
for one identifier. The full traceback rides in the event `extra`.

### Failure signatures

| Exception | Meaning |
|-----------|---------|
| `SentryInitError` | `sentry_sdk.init()` raised — bad DSN, or network unreachable during the init handshake |
| `SentryCaptureError` | the capture call raised |

<!-- src: shared/sentry_client.py:46-55 | verified 2026-07-14 -->
Both subclass `SentryError`. `error_log._alert_critical` wraps the capture in a
broad-except for failure isolation, so a Sentry failure never blocks the other two legs.

---

## Tailscale (network posture)

<!-- src: CLAUDE.md ("What NOT to do" + operator dashboard) | verified 2026-07-14 -->
Tailscale is the **only** way anything ITS-owned is reachable off the execution host.
The architectural rule is: **do not expose SSH or any service to the public internet —
Tailscale-only.** The one internal HTTP surface, the operator dashboard, binds to
`127.0.0.1:8484` (localhost-only FastAPI) and is exposed **only over Tailscale** — never
a public listener.

<!-- src: CLAUDE.md ("What NOT to do" — cloud-server execution) | verified 2026-07-14 -->
This has no client wrapper and no Keychain secret — it is a host/network configuration,
not an API integration. The architecture is **local-first on the MacBook through Phase
4**; there is no cloud-server execution to firewall. The public-facing surface that
*does* exist (the Safety Portal) is a separate Cloudflare Worker with its own auth (see
below), not an ITS host service.

---

## Healthchecks.io (external heartbeat / dead-man's switch)

<!-- src: shared/heartbeat_client.py (module docstring + ping) | verified 2026-07-14 -->
The external heartbeat is the **only detector for total-host failure** — a crash,
disk-full, launchd unload, or user logout that silences every in-tenant signal at once.
`scripts/watchdog.py` fires a single fire-and-forget `GET` to a configured monitor URL
once per daily run via `shared/heartbeat_client.ping`. If the monitor does not see the
ping within its configured period+grace, it alerts the operator out-of-band. This is
**audit F16**.

### Auth model & config

<!-- src: scripts/watchdog.py:2476-2496, shared/heartbeat_client.py (URL note) | verified 2026-07-14 -->
There is **no auth and no Keychain secret** — the ping URL is itself the write-only
credential. It is read from `ITS_Config` row `system.heartbeat_url` (Workstream
`global`) by the watchdog, **not** from Keychain. The watchdog skips the ping when the
value is unset or still the seeded placeholder `PLACEHOLDER_uptimerobot_heartbeat_url`
(so a fork that hasn't provisioned a monitor doesn't ping a dead URL).

| Item | Name | Notes |
|------|------|-------|
| Ping URL | `system.heartbeat_url` | `ITS_Config`, Workstream `global` — write-only beacon URL, not a secret |

### Known constraints & behavior

<!-- src: shared/heartbeat_client.py (ping docstring + failure modes) | verified 2026-07-14 -->
`ping()` is **fail-soft — it never raises**. A dead monitoring endpoint or network blip
must not break the watchdog's real checks. Any failure — connection refused, timeout, or
non-2xx (routed through `raise_for_status`) — is logged WARN under `error_log` category
`heartbeat_ping_failed` and the next daily run retries. Timeout is 10s.

<!-- src: scripts/watchdog.py:2460-2478 | verified 2026-07-14 -->
The ping fires on **every non-PAUSED run, including MAINTENANCE** — suppressing it during
maintenance would trip a false "host dead" alert. A PAUSED system skips all checks and
the ping (a deliberate operator pause is not host death).

> ### Naming note — resolved: the monitor is **Healthchecks.io**
> <!-- src: shared/heartbeat_client.py:1 (docstring), tests/test_heartbeat_client.py (hc-ping.com), scripts/seed_its_config.py (row Description) | verified 2026-07-26 -->
> This is **not** a coin-flip between two equivalent names. The shipped client, the live
> tests and the seeded ITS_Config row all say Healthchecks.io: `shared/heartbeat_client.py`
> line 1 is "Healthchecks.io heartbeat client", the tests ping `hc-ping.com`, and the
> `system.heartbeat_url` row's own Description reads "Healthchecks.io heartbeat URL pinged
> by scripts/watchdog.py" (`scripts/seed_its_config.py`). The operator's provisioning
> decision and its reason are recorded in
> `docs/session_logs/2026-05-28_f16-heartbeat-ping.md`: UptimeRobot's free tier gates
> heartbeat monitoring behind Pro and restricts commercial use, so a Healthchecks.io check
> was provisioned instead. Everywhere "UptimeRobot" still appears in exec-repo prose it is
> **stale prose**, not a second supported vendor.
>
> **Open cross-repo inconsistency (flagged, not resolved here):** blueprint doctrine still
> names UptimeRobot (`doctrine/vision-and-roadmap.md`), following a 2026-06-01 edit made on
> the grounds that "Healthchecks.io appears nowhere in the blueprint". Per CLAUDE.md the
> planning layer wins on doctrine, so this must be reconciled blueprint-side — it cannot be
> declared closed from this repo.
>
> **One deliberate exception — do not "fix" it.** The seeded placeholder literal
> `PLACEHOLDER_uptimerobot_heartbeat_url` is a **frozen token** that must stay
> byte-identical across `scripts/seed_its_config.py`, `scripts/watchdog.py`,
> `tests/test_watchdog.py` and `tests/test_heartbeat_client_integration.py` — the watchdog
> compares against it to decide whether the beacon is armed. Renaming it is a code change,
> not a doc change.

---

## Cloudflare (Workers + D1)

<!-- src: safety_portal/wrangler.jsonc:1-33 | verified 2026-07-14 -->
The Safety Portal is a **single Cloudflare Worker** (`its-safety-portal`) that serves the
built React SPA (static assets) **and** handles same-origin `/api/*` routes — zero CORS.
It is the **send-free D1 queue + HMAC-signing layer for all workstreams** (safety, PO,
subcontracts, field-ops): it durably queues and cryptographically signs each submission,
but **never sends anything externally**. The Mac-side daemons pull from it over HTTPS.

### Auth model (Worker ⇄ Mac)

<!-- src: safety_portal/worker/index.ts:177-221 | verified 2026-07-14 -->
The Worker's internal `/api/internal/*` routes are **Bearer-token gated** with
constant-time comparison and **fail-closed on a missing secret**. There is deliberate
**privilege separation** — a distinct token per Mac-side daemon class rather than one
shared token:

| Worker Secret (env) | Gate / consumer |
|---------------------|-----------------|
| `PORTAL_INTERNAL_API_TOKEN` | `/api/internal/*` — the `portal_poll` submission-drain daemon |
| `PORTAL_ADMIN_API_TOKEN` | `/api/internal/admin/*` — operator user-provisioning |
| `PORTAL_FIELDOPS_API_TOKEN` | `/api/internal/fieldops/*` — the field-ops mirror daemon |
| `PORTAL_PO_API_TOKEN` | `/api/po/internal/*` — the PO daemon |
| `PORTAL_CONFIG_API_TOKEN` | `/api/internal/config/*` — the §50 config actuator daemon |
| `PORTAL_SUB_API_TOKEN` | subcontract internal routes |
| `SESSION_SIGNING_SECRET` | HMAC key that signs portal session cookies (Hono `setSignedCookie`/`getSignedCookie`; distinct from the bcrypt `users.password_hash` used for password auth) |
| `HMAC_PAYLOAD_SECRET` | signs every queued submission/photo payload |

<!-- src: safety_portal/wrangler.jsonc:10-12 | verified 2026-07-14 -->
These are **Workers Secrets**, set with `wrangler secret put <NAME>` (or `.dev.vars`
locally) — **never** committed to `wrangler.jsonc`. On the Mac side, the matching
Keychain secrets are `ITS_PORTAL_INTERNAL_TOKEN` (the bearer) and `ITS_PORTAL_HMAC_SECRET`
(mirrors the Worker's `HMAC_PAYLOAD_SECRET`).

### The HMAC trust boundary

<!-- src: shared/portal_hmac.py:1-30 | verified 2026-07-14 -->
The Worker signs each submission; `portal_poll` **re-computes the canonical HMAC-SHA256
on the Mac before intake files it** (`shared/portal_hmac.py`). The canonical string is
`submission_uuid \n job_id \n form_code \n work_date \n payload_json`, and `payload_json`
is the **exact stored JSON string used verbatim, never re-serialized** (re-serialization
would change the bytes and break verify). Compare is constant-time (`hmac.compare_digest`),
and `verify` returns `False` (never raises) on any mismatch — the downgrade defense: a
failed submission is rejected + flagged, **not filed**.

<!-- src: shared/portal_hmac.py (item-photo + daily-photo protocols) | verified 2026-07-14 -->
Photo queues use the **same key** over **domain-separated** canonical strings — item
photos start with the literal `"item_photo:v1"`, daily photos with `"daily_photo:v1"` —
so cross-protocol signature confusion is structurally impossible (a submission canonical
starts with a UUID).

### Config, secrets & the pull model

| Item | Name | Notes |
|------|------|-------|
| Worker base URL | `safety_reports.portal.worker_base_url` | `ITS_Config` — the Worker origin; **fail-closed: if unset, the daemon does not poll** |
| Mac bearer | `ITS_PORTAL_INTERNAL_TOKEN` | Keychain — fail-closed if absent |
| Mac HMAC secret | `ITS_PORTAL_HMAC_SECRET` | Keychain — mirrors Worker `HMAC_PAYLOAD_SECRET` |

<!-- src: shared/portal_client.py:56-82 (route path constants); safety_portal/worker/index.ts (the /api/internal/pending SQL: oldest-first, box_verified=0) | verified 2026-07-15 -->
`portal_poll` drains `GET /api/internal/pending` (the Worker returns unfiled rows
oldest-first, `box_verified=0`), files each via intake, then POSTs
`/api/internal/mark-filed` as the receipt. Other internal
routes cover sync (`/api/internal/sync`), rejected receipts, filed-PDF download cache,
photo pools, publish/config claim-stamp, field-ops mirror, and prune status.

### D1 database

<!-- src: safety_portal/wrangler.jsonc:72-91 | verified 2026-07-14 -->
The Worker binds one D1 database as `DB` (`database_name = its-safety-portal-db`,
region ENAM). A **daily cron** (`0 9 * * *`) prunes the D1 **cache** — filed submissions
older than 90 days (the durable record is Box + the week sheet) and `audit_log` older
than a year. An **unfiled** submission (`box_verified=0`) is **never evicted**. Cron
triggers deploy with the Worker (`wrangler deploy`); there is no separate dashboard step.
R2 was removed 2026-06-05 — the Worker never holds a PDF; intake renders it and stores it
in Box, and the Worker only signs + queues structured data.

### Deploy constraints (the footguns)

<!-- src: safety_portal/wrangler.jsonc:57-70 | verified 2026-07-14 -->
The portal serves on the **custom domain** `safety.evergreenmirror.com`
(`custom_domain: true`). **Footgun:** `custom_domain: true` **disables the
`*.workers.dev` URL on deploy** (error 1042) unless `workers_dev: true` is also set. The
portal is intentionally custom-domain-only, so `safety_reports.portal.worker_base_url`
must point at `https://safety.evergreenmirror.com`, and must be **repointed immediately
after any deploy that toggles this route**.

<!-- src: CLAUDE.md ("Don't deploy/migrate from a stale checkout") | verified 2026-07-14 -->
**Always `git -C ~/its pull origin main` to latest before any `wrangler deploy` or
`wrangler d1 migrations apply/list`.** A 25-commit-behind checkout once reported "No
migrations to apply" while the live Worker expected the newer tables — the 2026-06-28
universal portal lockout. The `block-stale-cloudflare-deploy.sh` hook plus watchdog
Checks Q and S catch the in-session and post-merge cases. `npm run deploy` is
`vite build && wrangler deploy`; a "nothing changed" result is usually browser cache —
verify the asset hash changed in the deploy output, then hard-refresh.

### Health / verify

<!-- src: scripts/watchdog.py:1900-1971 (Check V) + CLAUDE.md (Checks Q/R/S) | verified 2026-07-14 -->
Portal health is watched by several checks: **Check Q** (portal-poll fetch outage),
**Check R** (portal-poll backlog), **Check S** (main-branch CI green on the merge
commit), and **Check V** (D1 prune heartbeat via `GET /api/internal/prune-status` —
stale → WARN, failed-stage/size → CRITICAL). A portal fetch outage is a low-class
"confirm the Worker is up and restart the daemon" repair; a bearer/token rotation is
high-class (escalate to Seth).

---

## Secret-name quick reference

<!-- src: shared/*_client.py + safety_portal/worker/index.ts (all verified above) | verified 2026-07-14 -->
Every name below is a **Keychain entry name** (Mac) or a **Workers Secret name**
(Cloudflare) — never a value. Rotation is a ceremony against the named store.

| Integration | Keychain (Mac) | Workers Secret (Cloudflare) | `ITS_Config` key |
|-------------|----------------|-----------------------------|------------------|
| Microsoft Graph | `ITS_MS_TENANT_ID`, `ITS_MS_CLIENT_ID`, `ITS_MS_CLIENT_SECRET` | — | — |
| Box | `ITS_BOX_CLIENT_ID`, `ITS_BOX_CLIENT_SECRET`, `ITS_BOX_REFRESH_TOKEN` | — | — |
| Smartsheet | `ITS_SMARTSHEET_TOKEN` | — | `circuit_breaker.*` |
| Resend | `ITS_RESEND_API_KEY` | — | `system.resend_api_keychain_key`, `system.operator_email` |
| Sentry | `ITS_SENTRY_DSN` | — | `system.sentry_dsn_keychain_key` |
| Anthropic | `ITS_ANTHROPIC_KEY` | — | — |
| Heartbeat | — (URL is the credential) | — | `system.heartbeat_url` |
| Cloudflare portal | `ITS_PORTAL_INTERNAL_TOKEN`, `ITS_PORTAL_HMAC_SECRET` | `PORTAL_INTERNAL_API_TOKEN`, `PORTAL_ADMIN_API_TOKEN`, `PORTAL_FIELDOPS_API_TOKEN`, `PORTAL_PO_API_TOKEN`, `PORTAL_CONFIG_API_TOKEN`, `PORTAL_SUB_API_TOKEN`, `SESSION_SIGNING_SECRET`, `HMAC_PAYLOAD_SECRET` | `safety_reports.portal.worker_base_url` |

## Watchdog check quick reference (integration health)

<!-- src: scripts/watchdog.py (Checks L, P, Q, R, S, V) + CLAUDE.md | verified 2026-07-14 -->

| Check | Integration | What it proves |
|-------|-------------|----------------|
| L | Smartsheet | `ITS_SMARTSHEET_TOKEN` can WRITE (create+delete a throwaway sheet), not just read |
| P | Box | Refresh token exercised inside 60d — WARN at 50d idle, CRITICAL at 58d |
| Q | Cloudflare | portal-poll fetch outage (Worker unreachable / bearer rejected / base URL wrong) |
| R | Cloudflare | portal-poll backlog (submissions queued but not draining) |
| S | Cloudflare/CI | main-branch CI green on the merge commit |
| V | Cloudflare/D1 | D1 prune heartbeat — stale WARN, failed-stage/size CRITICAL |
| (F16 ping) | Heartbeat | total-host death — external monitor alerts if the daily ping is missed |

## Edge cases & limitations

<!-- src: shared/resend_client.py:1-16, shared/sentry_client.py:1-16 | verified 2026-07-14 -->
- **The three alert legs are independent by design.** Resend, Sentry, and the Smartsheet
  `ITS_Errors` write each fire under their own broad-except so one failing never blocks
  the others; a shared `correlation_id` ties them together for triage.
- **Two config names carry a "keychain-name indirection" (`system.resend_api_keychain_key`,
  `system.sentry_dsn_keychain_key`).** The module *docstrings* describe these as the
  source of the Keychain entry name, but the shipped code reads the fixed names
  `ITS_RESEND_API_KEY` / `ITS_SENTRY_DSN` directly. Treat the fixed names as
  authoritative for a rotation until the indirection is exercised.
- **Anthropic has no failure-signature section here** because it has only one live caller
  (`safety_reports/intake.py`) and no dedicated client test; it is covered via
  `tests/test_intake.py`, which mocks `anthropic_client.call`.

## Related docs

- `system_architecture.md` — how these integrations compose into the two-layer system.
- `daemon_reference.md` — the daemons that consume each integration and their intervals.
- `data_model_reference.md` — the Smartsheet sheets and D1 tables these clients read/write.
- `security_trust_model.md` — the External Send Gate, adversarial input handling, and the HMAC boundary.
- `escalation_matrix.md` — Tier-2 vs. Tier-3 boundaries for a failed integration (esp. Box token re-seed).
- `glossary.md` — terms (triple-fire, circuit breaker, kill switch, pull model).
- `documentation_index.md` — the full Tier-1 documentation corpus index.
