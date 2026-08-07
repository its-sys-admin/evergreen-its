"""Portal-submission HMAC — the Python verify side of the Phase-5 pull-model trust boundary.

Purpose
-------
    Mirror the Cloudflare Worker's signing (safety_portal/worker/index.ts —
    canonicalPayload + hmacHex) on the Python side. The Worker signs each submission
    at /api/submit; the portal_poll daemon (Phase 5) verifies it before intake files
    the submission.

Invariants
----------
    * The canonical payload + HMAC-SHA256-hex MUST match the Worker byte-for-byte:
          canonical = submission_uuid \\n job_id \\n form_code \\n work_date \\n payload_json
          hmac      = HMAC-SHA256(secret, canonical).hexdigest()   (lowercase)
      `payload_json` is the EXACT JSON string the Worker stored — used verbatim,
      NEVER re-serialized (re-serialization would change the bytes and break verify).
    * Constant-time compare (hmac.compare_digest) — no timing oracle.

Failure modes
-------------
    `verify` returns False (never raises) on any mismatch — wrong secret, tampered
    field, or absent/empty signature. A False result is the downgrade defense: the
    caller (portal_poll) rejects + flags the submission and does NOT file it.

Consumers
---------
    safety_reports/portal_poll.py (Phase 5) — verifies every pulled submission before
    handing it to intake. The secret is the macOS Keychain `ITS_PORTAL_HMAC_SECRET`,
    mirroring the Worker's HMAC_PAYLOAD_SECRET.

Item-photo protocol (G1 Slice 2)
--------------------------------
    The checklist item-photo queue (`item_photos`, migration 0036) is signed by the
    Worker with the SAME key + MAC (HMAC-SHA256 → lowercase hex) over a DIFFERENT,
    domain-separated canonical string (fieldops_checklist.ts `itemPhotoCanonical`):

        canonical = "item_photo:v1" \\n <item_state_id (decimal)> \\n <photo_json>

    * The `"item_photo:v1"` literal domain-separates this protocol from submission
      HMACs (a submission canonical starts with its uuid — cross-protocol signature
      confusion is structurally impossible) and versions the string.
    * `item_state_id` binds the photo to its item (a valid signed photo cannot be
      replayed onto a different item without failing verification).
    * `photo_json` is the EXACT stored JSON string ({data,name,taken_at,gps,
      uploaded_by}) — used VERBATIM, never re-serialized, exactly like payload_json.

    `verify_item_photo` is the Mac-side recompute portal_poll's `_service_item_photos`
    pass runs before any byte is screened or filed (the downgrade defense, mirroring
    the submission drain).

Daily-photo protocol (DR-photo-pool Slice 2)
--------------------------------------------
    The daily-report additional-photo POOL (`daily_photo_pool`, migration 0037) is
    signed by the Worker with the SAME key + MAC over its own domain-separated
    canonical string (fieldops_daily_photos.ts `dailyPhotoCanonical`):

        canonical = "daily_photo:v1" \\n <job_id> \\n <work_date> \\n <photo_json>

    * The `"daily_photo:v1"` literal domain-separates this protocol from submission
      HMACs (uuid-first) AND item-photo HMACs ("item_photo:v1") — cross-protocol
      signature confusion is structurally impossible — and versions the string.
    * `job_id` + `work_date` bind the photo to its day (a valid signed photo cannot
      be replayed onto a different job or date without failing verification). The
      pool row id can't participate — it doesn't exist until the INSERT the
      signature rides in.
    * `photo_json` is the EXACT stored JSON string ({data,name,taken_at,gps,
      uploaded_by}) — used VERBATIM, never re-serialized, exactly like payload_json.

    `verify_daily_photo` is the Mac-side recompute portal_poll's
    `_service_daily_photos` pass runs before any byte is screened or filed.

Purchase-order protocol (PO S4)
-------------------------------
    A generated PO (`purchase_orders`, migration 0043) is signed by the Worker at
    /api/po/drafts/:id/generate with the SAME key + MAC over its own
    domain-separated canonical string (safety_portal/worker/po.ts
    `poCanonicalString` + `canonicalPoJson`):

        canonical = "po:v1" \\n <po_id (decimal)> \\n <po_number> \\n <canonical_json>

    * The `"po:v1"` literal domain-separates this protocol from submission HMACs
      (uuid-first), item-photo HMACs ("item_photo:v1"), and daily-photo HMACs
      ("daily_photo:v1") — cross-protocol signature confusion is structurally
      impossible — and versions the string.
    * `po_id` + `po_number` bind the signature to one allocated PO identity (a
      signed body cannot be replayed under a different PO number or D1 row).
    * `canonical_json` is UNLIKE payload_json: the Worker does NOT store it — it
      is REBUILT on both sides from the row's fields in a FIXED key order
      (`canonicalPoJson`'s insertion order, mirrored by `PO_CANONICAL_HEADER_KEYS`
      + `PO_CANONICAL_LINE_KEYS` below) and serialized with JSON.stringify
      semantics (`json.dumps(..., ensure_ascii=False, separators=(",", ":"))`).
      Byte-matching therefore depends on (a) the key order, (b) compact
      separators, (c) raw (non-\\u-escaped) non-ASCII, and (d) values passing
      through VERBATIM from the Worker's JSON response — the wire values were
      serialized by JS once already, so a parse→re-dump round-trip in Python
      reproduces the exact number/string forms (ints stay ints; the ≤3dp qty
      double's shortest-roundtrip repr agrees across JS/Python). The
      cross-language vector is pinned in tests/test_portal_hmac_po.py.

    `verify_po` is the Mac-side recompute `po_materials.po_poll`'s drafts pass
    runs before any render/Box-file/mark-filed (the downgrade defense, mirroring
    the submission drain).

Subcontract protocol (SC S3c)
-----------------------------
    A generated subcontract (`subcontracts`, migration 0050) is signed by the
    Worker at /api/subcontracts/drafts/:id/generate with the SAME key + MAC over
    its own domain-separated canonical string (safety_portal/worker/subcontract.ts
    `subCanonicalString` + `canonicalSubJson`):

        canonical = "sub:v1" \\n <sc_id (decimal)> \\n <sc_number> \\n <canonical_json>

    * The `"sub:v1"` literal domain-separates this protocol from submission HMACs
      (uuid-first), item-photo ("item_photo:v1"), daily-photo ("daily_photo:v1"),
      and PO ("po:v1") HMACs — cross-protocol signature confusion is structurally
      impossible — and versions the string.
    * `sc_id` + `sc_number` bind the signature to one allocated subcontract identity
      (a signed body cannot be replayed under a different subcontract number or D1
      row), exactly as PO binds `po_id` + `po_number`.
    * `canonical_json` is REBUILT on both sides from the row's fields in a FIXED key
      order (`canonicalSubJson`'s insertion order, mirrored by
      `SUB_CANONICAL_HEADER_KEYS` + `SUB_CANONICAL_LINE_KEYS` below) and serialized
      with JSON.stringify semantics (`json.dumps(..., ensure_ascii=False,
      separators=(",", ":"), allow_nan=False)`) — byte-matching depends on key order,
      compact separators, raw non-ASCII, and verbatim wire values, identical to the
      PO protocol. The `sov_lines` nest key (NOT PO's `line_items`) matches the D1
      table + the /pending row key. The cross-language vector is pinned in
      tests/test_portal_hmac_sub.py.

    `verify_sub` is the Mac-side recompute `subcontracts.subcontract_poll`'s drafts
    pass runs before any render/Box-file/mark-filed (the downgrade defense,
    mirroring the submission drain).

PO-attachment protocol (Feature B)
----------------------------------
    A draft-time PO document attachment (`po_attachments`, migration 0053) is signed
    by the Worker at upload (safety_portal/worker/po_attachments.ts
    `poAttachmentCanonical`) with the SAME key + MAC over its own domain-separated
    canonical string:

        canonical = "po-att:v1" \\n <att_uuid> \\n <po_id (decimal)> \\n <filename>
                    \\n <declared_mime> \\n <size_bytes (decimal)> \\n <sha256 hex>

    Unlike the other protocols the CONTENT itself is signature-covered: `sha256` is
    the Worker-computed digest of the decoded bytes, so the Mac's recompute over the
    reassembled chunks extends the signature to the bytes. `verify_po_attachment` is
    the recompute `po_materials.po_poll`'s attachment pass runs before a single byte
    is §34-screened or filed.

Vendor-estimate protocol (ADR-0004 E1/E2)
-----------------------------------------
    An office-uploaded vendor estimate (`po_estimates`, migration 0054) is signed
    by the Worker at upload (safety_portal/worker/po_estimates.ts
    `estimateCanonical`) with the SAME key + MAC over its own domain-separated
    canonical string:

        canonical = "est:v1" \\n <est_uuid> \\n <job_no> \\n <filename>
                    \\n <declared_mime> \\n <size_bytes (decimal)> \\n <sha256 hex>

    The shape is the po-att:v1 contract with the binding identity swapped: an
    estimate is bound to a JOB (`job_no` — it exists BEFORE any PO), not a PO row.
    Like po-att:v1 the CONTENT is signature-covered (`size_bytes` + `sha256` of the
    decoded bytes), so the Mac's recompute over the reassembled chunks extends the
    signature to the bytes themselves. `verify_po_estimate` is the recompute
    `po_materials.estimate_poll` runs before a single hostile byte is §34-screened,
    classified, or filed (the downgrade defense).

RFQ quote-form protocol (ADR-0004 decision 10, E6/PR-B)
-------------------------------------------------------
    The Tier-0 fillable `.xlsx` quote form (`po_materials/quote_form.py`) carries a
    hidden `_ITS_META` sheet whose `ITS_FORM_TOKEN` defined name holds a MAC over
    its own domain-separated canonical string:

        canonical = "rfq-form:v1" \\n <rfq_number> \\n <vendor_key>

    * The `"rfq-form:v1"` literal domain-separates this protocol from every other
      protocol above — cross-protocol signature confusion is structurally
      impossible — and versions the string.
    * `rfq_number` + `vendor_key` bind the token to one (RFQ, vendor) identity: a
      valid token cannot be replayed onto a different RFQ or vendor without
      failing verification, so a VERIFIED round-tripped form may auto-bind the
      upload to its RFQ (the ADR decision-10 auto-bind; the disposition UI still
      shows the binding for human confirmation).
    * UNLIKE the content-covered protocols (po-att:v1 / est:v1) the token
      deliberately does NOT cover the form's cell contents — the vendor is
      SUPPOSED to fill the price cells. The token asserts identity only; every
      filled value stays untrusted data (Invariant 2) and re-enters the trusted
      path through parse hardening + the human disposition accept.

    `verify_rfq_form_token` is the constant-time recompute
    `quote_form.parse_quote_form` runs; an absent/tampered token degrades the
    upload to an ORDINARY ladder document (verified=False, no auto-bind) — never
    an error, never a file refusal by itself.

RFQ protocol (ADR-0004 R2)
--------------------------
    A composed request-for-quote (`rfqs`, migration 0056) is signed by the Worker at
    /api/po/rfqs/:id/generate with the SAME key + MAC over its own
    domain-separated canonical string (safety_portal/worker/rfq.ts
    `rfqCanonicalString` + `canonicalRfqJson`):

        canonical = "rfq:v1" \\n <rfq_id (decimal)> \\n <rfq_number> \\n <canonical_json>

    * The `"rfq:v1"` literal domain-separates this protocol from every sibling
      (submission uuid-first, "item_photo:v1", "daily_photo:v1", "po:v1", "sub:v1",
      "po-att:v1", "est:v1") — cross-protocol signature confusion is structurally
      impossible — and versions the string.
    * `rfq_id` + `rfq_number` bind the signature to one allocated RFQ identity (a
      signed body cannot be replayed under a different RFQ number or D1 row),
      exactly as PO binds `po_id` + `po_number`.
    * `canonical_json` is REBUILT on both sides from the row's fields in a FIXED key
      order (`RFQ_CANONICAL_HEADER_KEYS` + a `line_items` nest of
      `RFQ_CANONICAL_LINE_KEYS` + a SORTED `vendor_keys` array LAST, mirroring the
      Worker's `canonicalRfqJson` insertion order exactly) and serialized with
      JSON.stringify semantics (`json.dumps(..., ensure_ascii=False,
      separators=(",", ":"), allow_nan=False)`). **Why recompute-from-fields and not
      a verbatim stored string:** this MIRRORS the po:v1 / sub:v1 pattern EXACTLY —
      `po_poll` re-canonicalizes from the served fields (`po_canonical_json`)
      because (a) the Worker does not store the canonical string, and (b) the
      rebuild pattern signature-covers the exact served fields the Mac renders
      from, so a D1/serving-layer tamper of ANY rendered field fails the verify
      (a verbatim-string design would only cover the stored blob, not the sibling
      row fields the renderer actually consumes). The submission-payload verbatim
      pattern (`payload_json`) applies only where the signed string IS the stored
      artifact.
    * `vendor_keys` (the RFQ's vendor fan-out list) is signature-covered as a JSON
      array INSIDE canonical_json, SORTED ascending on both sides (vendor-row read
      order must never change the signature) — a tampered vendor list (recipient
      poisoning's only remaining surface after ADR-0004 decision 9) fails the verify.
    * The RFQ is PRICE-FREE by design: NO money field participates in the canonical
      (and none is served) — a request for quote carries no dollars.

    `verify_rfq` is the Mac-side recompute `po_materials.rfq_poll`'s pass ① runs
    before any render/Box-file/mark-filed (the downgrade defense, mirroring the PO
    drafts pass). The cross-language vector is pinned in tests/test_rfq_hmac_parity.py.

Material-manifest protocol (PR3b — the materials-tracking import pool)
---------------------------------------------------------------------
    An office-uploaded materials MANIFEST — a BOM or a shipping log (`job_manifests`,
    migration 0060) — is signed by the Worker at upload
    (safety_portal/worker/fieldops_manifests.ts `manifestCanonical`) with the SAME key
    + MAC over its own domain-separated canonical string:

        canonical = "manifest:v1" \\n <manifest_uuid> \\n <job_id> \\n <filename>
                    \\n <declared_mime> \\n <size_bytes (decimal)> \\n <sha256 hex>

    * The `"manifest:v1"` literal domain-separates this protocol from every sibling
      (submission uuid-first, "item_photo:v1", "daily_photo:v1", "po:v1", "sub:v1",
      "po-att:v1", "est:v1", "rfq-form:v1", "rfq:v1") — cross-protocol signature
      confusion is structurally impossible — and versions the string.
    * This is the po-att:v1 / est:v1 flat CONTENT-COVERED shape with the binding
      identity swapped: a manifest binds to a field-ops JOB (`job_id`), not to a PO
      row (`po_id`) or a procurement job number (`job_no`). `manifest_uuid` + `job_id`
      together mean a signed manifest cannot be replayed onto another row OR another
      job — which matters more here than in the estimate lane, because the manifest
      pool's dedupe is deliberately PER-JOB (one master BOM legitimately serves
      sibling jobs), so the same bytes legitimately exist under two `job_id`s.
    * Like its two content-covered siblings the CONTENT is signature-covered
      (`size_bytes` + `sha256` of the DECODED bytes), so the Mac's own digest
      recompute over the reassembled chunks extends the signature to the bytes
      themselves: tampered chunks fail the digest, a tampered digest fails the HMAC.
      The HMAC alone covers only the CLAIMS about the bytes — the length + digest
      recompute is a separate, equally mandatory check.

    `verify_manifest` is the recompute `field_ops.manifest_poll` runs before a single
    hostile byte is §34-screened, handed to the sandboxed extractor, parsed, or filed
    to Box (the downgrade defense). The cross-language vector is pinned in
    tests/test_manifest_hmac_parity.py.
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import json
from collections.abc import Mapping, Sequence
from typing import Any

# The item-photo protocol's domain-separation literal — MUST match the Worker's
# itemPhotoCanonical (safety_portal/worker/fieldops_checklist.ts) byte-for-byte.
ITEM_PHOTO_DOMAIN = "item_photo:v1"

# The daily-photo protocol's domain-separation literal — MUST match the Worker's
# dailyPhotoCanonical (safety_portal/worker/fieldops_daily_photos.ts) byte-for-byte.
DAILY_PHOTO_DOMAIN = "daily_photo:v1"

# The purchase-order protocol's domain-separation literal — MUST match the Worker's
# PO_HMAC_DOMAIN (safety_portal/worker/po.ts) byte-for-byte.
PO_DOMAIN = "po:v1"

# The FIXED header-field order of the Worker's canonicalPoJson (po.ts) — insertion
# order is the serialization order on both sides; any reorder breaks every signature.
PO_CANONICAL_HEADER_KEYS: tuple[str, ...] = (
    "po_number",
    "job_no",
    "site_phase",
    "supersede_seq",
    "revision",
    "vendor_key",
    "job_id",
    "job_name",
    "ship_to_name",
    "ship_to_address",
    "ship_to_city",
    "ship_to_state",
    "ship_to_zip",
    "delivery_contact_name",
    "delivery_contact_phone",
    "delivery_contact_email",
    "sow_text",
    "delivery_instructions",
    "payment_terms_text",
    "terms_profile_id",
    "terms_version",
    "subtotal_cents",
    "tax_mode",
    "tax_rate_bp",
    "tax_cents",
    "shipping_cents",
    "total_cents",
    "line_column_variant",
    "supersedes_po_id",
    "approver_name",
    "approver_title",
)

# The FIXED per-line key order of canonicalPoJson's line_items mapper (po.ts).
PO_CANONICAL_LINE_KEYS: tuple[str, ...] = (
    "position",
    "part_number",
    "description",
    "qty",
    "unit",
    "unit_cost_cents",
    "extended_cents",
    "watts",
    "panels",
    "pallets",
    "price_per_watt_microcents",
)


def canonical_payload(
    *, submission_uuid: str, job_id: str, form_code: str, work_date: str, payload_json: str
) -> str:
    """The exact string the Worker signs (order + ``\\n`` separator are load-bearing)."""
    return "\n".join([submission_uuid, job_id, form_code, work_date, payload_json])


def sign(
    secret: str, *, submission_uuid: str, job_id: str, form_code: str, work_date: str, payload_json: str
) -> str:
    """HMAC-SHA256(secret, canonical) → lowercase hex — identical to the Worker's hmacHex."""
    msg = canonical_payload(
        submission_uuid=submission_uuid, job_id=job_id, form_code=form_code,
        work_date=work_date, payload_json=payload_json,
    ).encode("utf-8")
    return _hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def verify(
    secret: str, provided_hmac: str | None, *,
    submission_uuid: str, job_id: str, form_code: str, work_date: str, payload_json: str,
) -> bool:
    """True iff `provided_hmac` matches the recomputed signature. Never raises (False on any mismatch)."""
    expected = sign(
        secret, submission_uuid=submission_uuid, job_id=job_id, form_code=form_code,
        work_date=work_date, payload_json=payload_json,
    )
    return _hmac.compare_digest(expected, provided_hmac or "")


# ---- Item-photo protocol (G1 Slice 2 — see module docstring) ---------------------


def item_photo_canonical(*, item_state_id: int, photo_json: str) -> str:
    """The exact string the Worker signs for one checklist item photo
    (fieldops_checklist.ts itemPhotoCanonical — order + ``\\n`` separator load-bearing)."""
    return "\n".join([ITEM_PHOTO_DOMAIN, str(item_state_id), photo_json])


def sign_item_photo(secret: str, *, item_state_id: int, photo_json: str) -> str:
    """HMAC-SHA256(secret, item-photo canonical) → lowercase hex — identical to the
    Worker's hmacHex over itemPhotoCanonical. `photo_json` is used VERBATIM."""
    msg = item_photo_canonical(item_state_id=item_state_id, photo_json=photo_json).encode("utf-8")
    return _hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def verify_item_photo(
    secret: str, provided_hmac: str | None, *, item_state_id: int, photo_json: str
) -> bool:
    """True iff `provided_hmac` matches the recomputed item-photo signature.
    Constant-time; never raises (False on any mismatch — the caller refuses the row)."""
    expected = sign_item_photo(secret, item_state_id=item_state_id, photo_json=photo_json)
    return _hmac.compare_digest(expected, provided_hmac or "")


# ---- Daily-photo protocol (DR-photo-pool Slice 2 — see module docstring) ----------


def daily_photo_canonical(*, job_id: str, work_date: str, photo_json: str) -> str:
    """The exact string the Worker signs for one daily-pool photo
    (fieldops_daily_photos.ts dailyPhotoCanonical — order + ``\\n`` separator
    load-bearing). job_id + work_date bind the photo to its day."""
    return "\n".join([DAILY_PHOTO_DOMAIN, job_id, work_date, photo_json])


def sign_daily_photo(secret: str, *, job_id: str, work_date: str, photo_json: str) -> str:
    """HMAC-SHA256(secret, daily-photo canonical) → lowercase hex — identical to the
    Worker's hmacHex over dailyPhotoCanonical. `photo_json` is used VERBATIM."""
    msg = daily_photo_canonical(
        job_id=job_id, work_date=work_date, photo_json=photo_json
    ).encode("utf-8")
    return _hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def verify_daily_photo(
    secret: str, provided_hmac: str | None, *, job_id: str, work_date: str, photo_json: str
) -> bool:
    """True iff `provided_hmac` matches the recomputed daily-photo signature.
    Constant-time; never raises (False on any mismatch — the caller refuses the row)."""
    expected = sign_daily_photo(
        secret, job_id=job_id, work_date=work_date, photo_json=photo_json
    )
    return _hmac.compare_digest(expected, provided_hmac or "")


# ---- Purchase-order protocol (PO S4 — see module docstring) ------------------------


def po_canonical_json(po: Mapping[str, Any], line_items: Sequence[Mapping[str, Any]]) -> str:
    """Rebuild the Worker's canonicalPoJson byte-for-byte from a /pending row.

    Values pass through VERBATIM (no coercion): the row arrived as JSON the Worker
    serialized, so `json.dumps` over the parsed values reproduces the exact bytes —
    `ensure_ascii=False` (JSON.stringify never \\u-escapes non-ASCII), compact
    separators, and the FIXED key orders above. A key absent from the row serializes
    as null exactly like a D1 NULL would; if that differs from what the Worker signed,
    the verify simply fails (fail-closed — the drift IS the signal, never a file).

    `allow_nan=False`: NaN/Infinity are not JSON — a row carrying one is malformed
    transport and the resulting ValueError surfaces to the caller's per-row fence
    rather than minting a canonical string the Worker could never have signed.
    """
    obj: dict[str, Any] = {key: po.get(key) for key in PO_CANONICAL_HEADER_KEYS}
    obj["line_items"] = [
        {key: line.get(key) for key in PO_CANONICAL_LINE_KEYS} for line in line_items
    ]
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def po_canonical_string(po_id: int, po_number: str, canonical_json: str) -> str:
    """The exact string the Worker signs for one generated PO
    (po.ts poCanonicalString — order + ``\\n`` separator load-bearing).
    po_id + po_number bind the signature to one allocated PO identity."""
    return "\n".join([PO_DOMAIN, str(po_id), po_number, canonical_json])


def sign_po(secret: str, *, po_id: int, po_number: str, canonical_json: str) -> str:
    """HMAC-SHA256(secret, PO canonical) → lowercase hex — identical to the Worker's
    hmacHex over poCanonicalString. `canonical_json` comes from `po_canonical_json`."""
    msg = po_canonical_string(po_id, po_number, canonical_json).encode("utf-8")
    return _hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def verify_po(
    secret: str, provided_hmac: str | None, *, po_id: int, po_number: str, canonical_json: str
) -> bool:
    """True iff `provided_hmac` matches the recomputed PO signature.
    Constant-time; never raises (False on any mismatch — the caller refuses the row:
    one-shot flag + CRITICAL, never rendered, never filed, never marked)."""
    expected = sign_po(secret, po_id=po_id, po_number=po_number, canonical_json=canonical_json)
    return _hmac.compare_digest(expected, provided_hmac or "")


# ---- Subcontract protocol (SC S3c — see module docstring) --------------------------

# The subcontract protocol's domain-separation literal — MUST match the Worker's
# SUB_HMAC_DOMAIN (safety_portal/worker/subcontract.ts) byte-for-byte.
SUB_DOMAIN = "sub:v1"

# The FIXED header-field order of the Worker's canonicalSubJson (subcontract.ts) —
# insertion order is the serialization order on both sides; any reorder breaks every
# signature. All 31 canonical-signed business fields (migration 0050) ride here,
# frozen at draft; stored-but-not-currently-rendered columns (retainage_bp,
# subtotal_cents, site_name, site_address, trade, scope_summary, the exhibit_a_*
# trio, template_family) stay in the tuple so a draft edit to them is signature-
# covered — do NOT drop them.
SUB_CANONICAL_HEADER_KEYS: tuple[str, ...] = (
    "sc_number",
    "job_no",
    "site_phase",
    "supersede_seq",
    "revision",
    "sub_key",
    "trade",
    "job_id",
    "job_name",
    "project_name",
    "owner_entity",
    "prime_contractor",
    "site_name",
    "site_address",
    "governing_law_state",
    "exhibit_a_template_id",
    "exhibit_a_template_version",
    "exhibit_a_work_text",
    "scope_summary",
    "price_basis",
    "contract_price_cents",
    "retainage_bp",
    "subtotal_cents",
    "start_date",
    "completion_date",
    "terms_profile_id",
    "terms_version",
    "template_family",
    "supersedes_sc_id",
    "approver_name",
    "approver_title",
)

# The FIXED per-line key order of canonicalSubJson's sov_lines mapper (subcontract.ts).
# Delta vs PO_CANONICAL_LINE_KEYS: part_number→item_number, unit_cost_cents→
# unit_price_cents; the per-watt quartet (watts/panels/pallets/price_per_watt_
# microcents) is DROPPED (no per-watt module for subcontracts). `extended_cents` is
# ALWAYS server-computed = round(qty × unit_price_cents).
SUB_CANONICAL_LINE_KEYS: tuple[str, ...] = (
    "position",
    "item_number",
    "description",
    "qty",
    "unit",
    "unit_price_cents",
    "extended_cents",
)


def sub_canonical_json(sub: Mapping[str, Any], sov_lines: Sequence[Mapping[str, Any]]) -> str:
    """Rebuild the Worker's canonicalSubJson byte-for-byte from a /pending row.

    Values pass through VERBATIM (no coercion): the row arrived as JSON the Worker
    serialized, so `json.dumps` over the parsed values reproduces the exact bytes —
    `ensure_ascii=False` (JSON.stringify never \\u-escapes non-ASCII), compact
    separators, and the FIXED key orders above. A key absent from the row serializes
    as null exactly like a D1 NULL would; if that differs from what the Worker signed,
    the verify simply fails (fail-closed — the drift IS the signal, never a file).
    Line items nest under `sov_lines` (NOT PO's `line_items`), matching the D1 table.

    `allow_nan=False`: NaN/Infinity are not JSON — a row carrying one is malformed
    transport and the resulting ValueError surfaces to the caller's per-row fence
    rather than minting a canonical string the Worker could never have signed.
    """
    obj: dict[str, Any] = {key: sub.get(key) for key in SUB_CANONICAL_HEADER_KEYS}
    obj["sov_lines"] = [
        {key: line.get(key) for key in SUB_CANONICAL_LINE_KEYS} for line in sov_lines
    ]
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def sub_canonical_string(sc_id: int, sc_number: str, canonical_json: str) -> str:
    """The exact string the Worker signs for one generated subcontract
    (subcontract.ts subCanonicalString — order + ``\\n`` separator load-bearing).
    sc_id + sc_number bind the signature to one allocated subcontract identity."""
    return "\n".join([SUB_DOMAIN, str(sc_id), sc_number, canonical_json])


def sign_sub(secret: str, *, sc_id: int, sc_number: str, canonical_json: str) -> str:
    """HMAC-SHA256(secret, subcontract canonical) → lowercase hex — identical to the
    Worker's hmacHex over subCanonicalString. `canonical_json` comes from
    `sub_canonical_json`."""
    msg = sub_canonical_string(sc_id, sc_number, canonical_json).encode("utf-8")
    return _hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def verify_sub(
    secret: str, provided_hmac: str | None, *, sc_id: int, sc_number: str, canonical_json: str
) -> bool:
    """True iff `provided_hmac` matches the recomputed subcontract signature.
    Constant-time; never raises (False on any mismatch — the caller refuses the row:
    one-shot flag + CRITICAL, never rendered, never filed, never marked)."""
    expected = sign_sub(secret, sc_id=sc_id, sc_number=sc_number, canonical_json=canonical_json)
    return _hmac.compare_digest(expected, provided_hmac or "")


# ---- PO-attachment protocol (Feature B — the §34 doc-attachment pool) ---------------

# The PO-attachment protocol's domain-separation literal — MUST match the Worker's
# PO_ATTACH_HMAC_DOMAIN (safety_portal/worker/po_attachments.ts) byte-for-byte.
# Domain-separates attachment signatures from submission (uuid-first), item-photo
# ("item_photo:v1"), daily-photo ("daily_photo:v1"), PO ("po:v1"), and subcontract
# ("sub:v1") signatures — cross-protocol confusion is structurally impossible.
PO_ATTACH_DOMAIN = "po-att:v1"


def po_attachment_canonical(
    *, att_uuid: str, po_id: int, filename: str, declared_mime: str,
    size_bytes: int, sha256: str,
) -> str:
    """The exact string the Worker signs for one uploaded PO attachment
    (po_attachments.ts poAttachmentCanonical — order + ``\\n`` separator load-bearing).

    Binds identity (`att_uuid` + `po_id` — a signed attachment cannot be replayed onto
    a different row or PO), naming (`filename` + `declared_mime` — the values the Mac
    screener + Box filing consume), and CONTENT (`size_bytes` + `sha256` of the decoded
    bytes) — so the caller's sha256 recompute over the reassembled chunks extends the
    signature to the bytes themselves: tampered chunks fail the digest, a tampered
    digest fails the HMAC.
    """
    return "\n".join([
        PO_ATTACH_DOMAIN, att_uuid, str(po_id), filename, declared_mime,
        str(size_bytes), sha256,
    ])


def sign_po_attachment(
    secret: str, *, att_uuid: str, po_id: int, filename: str, declared_mime: str,
    size_bytes: int, sha256: str,
) -> str:
    """HMAC-SHA256(secret, attachment canonical) → lowercase hex — identical to the
    Worker's hmacHex over poAttachmentCanonical."""
    msg = po_attachment_canonical(
        att_uuid=att_uuid, po_id=po_id, filename=filename, declared_mime=declared_mime,
        size_bytes=size_bytes, sha256=sha256,
    ).encode("utf-8")
    return _hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def verify_po_attachment(
    secret: str, provided_hmac: str | None, *, att_uuid: str, po_id: int, filename: str,
    declared_mime: str, size_bytes: int, sha256: str,
) -> bool:
    """True iff `provided_hmac` matches the recomputed attachment signature.
    Constant-time; never raises (False on any mismatch — the caller refuses the
    attachment: CRITICAL + security Review-Queue row, never screened, never filed)."""
    expected = sign_po_attachment(
        secret, att_uuid=att_uuid, po_id=po_id, filename=filename,
        declared_mime=declared_mime, size_bytes=size_bytes, sha256=sha256,
    )
    return _hmac.compare_digest(expected, provided_hmac or "")


# ---- Vendor-estimate protocol (ADR-0004 E1/E2 — the estimate upload pool) -----------

# The vendor-estimate protocol's domain-separation literal — MUST match the Worker's
# EST_HMAC_DOMAIN (safety_portal/worker/po_estimates.ts) byte-for-byte.
# Domain-separates estimate signatures from submission (uuid-first), item-photo
# ("item_photo:v1"), daily-photo ("daily_photo:v1"), PO ("po:v1"), subcontract
# ("sub:v1"), and PO-attachment ("po-att:v1") signatures — cross-protocol confusion
# is structurally impossible.
EST_DOMAIN = "est:v1"


def est_canonical(
    *, est_uuid: str, job_no: str, filename: str, declared_mime: str,
    size_bytes: int, sha256: str,
) -> str:
    """The exact string the Worker signs for one uploaded vendor estimate
    (po_estimates.ts estimateCanonical — order + ``\\n`` separator load-bearing).

    Binds identity (`est_uuid` + `job_no` — a signed estimate cannot be replayed onto
    a different row or job), naming (`filename` + `declared_mime` — the values the Mac
    screener + classifier + Box filing consume), and CONTENT (`size_bytes` + `sha256`
    of the decoded bytes) — so the caller's sha256 recompute over the reassembled
    chunks extends the signature to the bytes themselves: tampered chunks fail the
    digest, a tampered digest fails the HMAC. The po-att:v1 shape with the binding
    identity swapped from PO row to job.
    """
    return "\n".join([
        EST_DOMAIN, est_uuid, job_no, filename, declared_mime,
        str(size_bytes), sha256,
    ])


def sign_po_estimate(
    secret: str, *, est_uuid: str, job_no: str, filename: str, declared_mime: str,
    size_bytes: int, sha256: str,
) -> str:
    """HMAC-SHA256(secret, estimate canonical) → lowercase hex — identical to the
    Worker's hmacHex over estimateCanonical."""
    msg = est_canonical(
        est_uuid=est_uuid, job_no=job_no, filename=filename, declared_mime=declared_mime,
        size_bytes=size_bytes, sha256=sha256,
    ).encode("utf-8")
    return _hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def verify_po_estimate(
    secret: str, provided_hmac: str | None, *, est_uuid: str, job_no: str, filename: str,
    declared_mime: str, size_bytes: int, sha256: str,
) -> bool:
    """True iff `provided_hmac` matches the recomputed estimate signature.
    Constant-time; never raises (False on any mismatch — the caller refuses the
    estimate: CRITICAL + security Review-Queue row, never screened, never filed)."""
    expected = sign_po_estimate(
        secret, est_uuid=est_uuid, job_no=job_no, filename=filename,
        declared_mime=declared_mime, size_bytes=size_bytes, sha256=sha256,
    )
    return _hmac.compare_digest(expected, provided_hmac or "")


# ---- RFQ quote-form protocol (ADR-0004 decision 10 — the Tier-0 form identity) ------

# The quote-form protocol's domain-separation literal — versioned; MUST match what
# po_materials/quote_form.py embeds byte-for-byte. Domain-separates form-identity
# tokens from every other protocol in this module (see the module docstring).
RFQ_FORM_DOMAIN = "rfq-form:v1"


def _as_secret_bytes(secret: str | bytes) -> bytes:
    """The Keychain secret is stored as a str; quote_form callers may hold bytes.
    Both encode to the same MAC key (utf-8)."""
    return secret.encode("utf-8") if isinstance(secret, str) else secret


def rfq_form_canonical(*, rfq_number: str, vendor_key: str) -> str:
    """The exact string the form token signs (order + ``\\n`` separator load-bearing).
    rfq_number + vendor_key bind the token to one (RFQ, vendor) identity."""
    return "\n".join([RFQ_FORM_DOMAIN, rfq_number, vendor_key])


def rfq_form_token(secret: str | bytes, rfq_number: str, vendor_key: str) -> str:
    """HMAC-SHA256(secret, rfq-form canonical) → lowercase hex — the value embedded
    in the hidden `_ITS_META` sheet's `ITS_FORM_TOKEN` defined name at render time
    and recomputed at parse time. Identity-only: the form's fillable CONTENT is
    deliberately outside the MAC (see the module docstring)."""
    msg = rfq_form_canonical(rfq_number=rfq_number, vendor_key=vendor_key).encode("utf-8")
    return _hmac.new(_as_secret_bytes(secret), msg, hashlib.sha256).hexdigest()


def verify_rfq_form_token(
    secret: str | bytes, provided_token: str | None, *, rfq_number: str, vendor_key: str
) -> bool:
    """True iff `provided_token` matches the recomputed form-identity token.
    Constant-time; never raises (False on any mismatch — the caller degrades the
    upload to an ORDINARY ladder document: verified=False, no RFQ auto-bind)."""
    expected = rfq_form_token(secret, rfq_number, vendor_key)
    return _hmac.compare_digest(expected, provided_token or "")


# ---- RFQ protocol (ADR-0004 R2 — the outbound request-for-quote lane) ---------------

# The RFQ protocol's domain-separation literal — MUST match the Worker's
# RFQ_HMAC_DOMAIN (safety_portal/worker/rfq.ts) byte-for-byte.
# Domain-separates RFQ signatures from submission (uuid-first), item-photo
# ("item_photo:v1"), daily-photo ("daily_photo:v1"), PO ("po:v1"), subcontract
# ("sub:v1"), PO-attachment ("po-att:v1"), and estimate ("est:v1") signatures —
# cross-protocol confusion is structurally impossible.
RFQ_DOMAIN = "rfq:v1"

# The FIXED header-field order of the Worker's canonicalRfqJson (rfq.ts) — insertion
# order is the serialization order on both sides; any reorder breaks every signature.
# PRICE-FREE by design: no money key exists in an RFQ (the Worker's vitest suite
# red-lines any *_cents/price/cost key ever appearing; tests/test_rfq_hmac_parity.py
# pins the same on this side).
RFQ_CANONICAL_HEADER_KEYS: tuple[str, ...] = (
    "rfq_number",
    "job_no",
    "job_name",
    "ship_to_name",
    "ship_to_address",
    "ship_to_city",
    "ship_to_state",
    "ship_to_zip",
    "delivery_contact_name",
    "delivery_contact_phone",
    "delivery_contact_email",
    "scope_text",
    "due_date",
)

# The FIXED per-line key order of canonicalRfqJson's line_items mapper (rfq.ts).
# Delta vs PO_CANONICAL_LINE_KEYS: NO unit_cost_cents / extended_cents / per-watt
# quartet (the RFQ line table is price-free); `line_note` is the per-line free-text
# column the vendor quotes against; qty is a ≤3dp double OR null ("quote per unit").
RFQ_CANONICAL_LINE_KEYS: tuple[str, ...] = (
    "position",
    "part_number",
    "description",
    "qty",
    "unit",
    "line_note",
)


def rfq_canonical_json(
    rfq: Mapping[str, Any],
    line_items: Sequence[Mapping[str, Any]],
    vendor_keys: Sequence[str],
) -> str:
    """Rebuild the Worker's canonicalRfqJson byte-for-byte from a /pending row.

    Values pass through VERBATIM (no coercion): the row arrived as JSON the Worker
    serialized, so `json.dumps` over the parsed values reproduces the exact bytes —
    `ensure_ascii=False` (JSON.stringify never \\u-escapes non-ASCII), compact
    separators, and the FIXED key orders above (recompute-from-fields, the po:v1 /
    sub:v1 pattern — see the module docstring for why NOT a stored verbatim string).
    A key absent from the row serializes as null exactly like a D1 NULL would; if
    that differs from what the Worker signed, the verify simply fails (fail-closed —
    the drift IS the signal, never a file). Line items nest under `line_items`;
    `vendor_keys` nests LAST and is SORTED ascending on BOTH sides (rfq.ts sorts at
    signing — vendor-row read order must never change the signature).

    `allow_nan=False`: NaN/Infinity are not JSON — a row carrying one is malformed
    transport and the resulting ValueError surfaces to the caller's per-row fence
    rather than minting a canonical string the Worker could never have signed.
    """
    obj: dict[str, Any] = {key: rfq.get(key) for key in RFQ_CANONICAL_HEADER_KEYS}
    obj["line_items"] = [
        {key: line.get(key) for key in RFQ_CANONICAL_LINE_KEYS} for line in line_items
    ]
    obj["vendor_keys"] = sorted(str(k) for k in vendor_keys)
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def rfq_canonical(rfq_id: int, rfq_number: str, canonical_json: str) -> str:
    """The exact string the Worker signs for one composed RFQ
    (rfq.ts rfqCanonicalString — order + ``\\n`` separator load-bearing).
    rfq_id + rfq_number bind the signature to one allocated RFQ identity."""
    return "\n".join([RFQ_DOMAIN, str(rfq_id), rfq_number, canonical_json])


def sign_rfq(secret: str, *, rfq_id: int, rfq_number: str, canonical_json: str) -> str:
    """HMAC-SHA256(secret, RFQ canonical) → lowercase hex — identical to the Worker's
    hmacHex over rfqCanonicalString. `canonical_json` comes from `rfq_canonical_json`."""
    msg = rfq_canonical(rfq_id, rfq_number, canonical_json).encode("utf-8")
    return _hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def verify_rfq(
    secret: str, provided_hmac: str | None, *, rfq_id: int, rfq_number: str, canonical_json: str
) -> bool:
    """True iff `provided_hmac` matches the recomputed RFQ signature.
    Constant-time; never raises (False on any mismatch — the caller refuses the row:
    one-shot flag + CRITICAL, never rendered, never filed, never marked)."""
    expected = sign_rfq(secret, rfq_id=rfq_id, rfq_number=rfq_number, canonical_json=canonical_json)
    return _hmac.compare_digest(expected, provided_hmac or "")


# ---- Material-manifest protocol (PR3b — the materials-tracking import pool) ----------

# The manifest protocol's domain-separation literal — MUST match the Worker's
# MANIFEST_HMAC_DOMAIN (safety_portal/worker/fieldops_manifests.ts) byte-for-byte.
# Domain-separates manifest signatures from submission (uuid-first), item-photo
# ("item_photo:v1"), daily-photo ("daily_photo:v1"), PO ("po:v1"), subcontract
# ("sub:v1"), PO-attachment ("po-att:v1"), estimate ("est:v1"), quote-form
# ("rfq-form:v1") and RFQ ("rfq:v1") signatures — cross-protocol confusion is
# structurally impossible.
MANIFEST_DOMAIN = "manifest:v1"


def manifest_canonical(
    *,
    manifest_uuid: str,
    job_id: str,
    filename: str,
    declared_mime: str,
    size_bytes: int,
    sha256: str,
) -> str:
    """The exact string the Worker signs for one uploaded materials manifest
    (fieldops_manifests.ts ``manifestCanonical`` — field order + the ``\\n``
    separator are load-bearing).

    Binds identity (``manifest_uuid`` + ``job_id`` — a signed manifest cannot be
    replayed onto a different row or a different job, which matters here because the
    pool's dedupe is per-JOB and one master BOM legitimately serves sibling jobs),
    naming (``filename`` + ``declared_mime`` — the values the §34 screener, the
    extractor dispatch and the Box filing all consume), and CONTENT (``size_bytes`` +
    ``sha256`` of the DECODED bytes) — so the caller's own sha256 recompute over the
    reassembled chunks extends the signature to the bytes themselves: tampered chunks
    fail the digest, a tampered digest fails the HMAC.

    ``size_bytes`` renders via ``str(int)`` to mirror the Worker's ``String(sizeBytes)``.
    That equivalence holds for ints ONLY — a float ``48213.0`` renders ``"48213.0"`` in
    Python and ``"48213"`` in JS, silently breaking every signature — so callers must
    reject a non-int before signing/verifying (``manifest_poll`` coerces to ``-1``,
    which fails closed).
    """
    return "\n".join([
        MANIFEST_DOMAIN,
        manifest_uuid,
        job_id,
        filename,
        declared_mime,
        str(size_bytes),
        sha256,
    ])


def sign_manifest(
    secret: str,
    *,
    manifest_uuid: str,
    job_id: str,
    filename: str,
    declared_mime: str,
    size_bytes: int,
    sha256: str,
) -> str:
    """HMAC-SHA256(secret, manifest canonical) → lowercase hex — identical to the
    Worker's ``hmacHex`` over ``manifestCanonical``."""
    msg = manifest_canonical(
        manifest_uuid=manifest_uuid,
        job_id=job_id,
        filename=filename,
        declared_mime=declared_mime,
        size_bytes=size_bytes,
        sha256=sha256,
    ).encode("utf-8")
    return _hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def verify_manifest(
    secret: str,
    provided_hmac: str | None,
    *,
    manifest_uuid: str,
    job_id: str,
    filename: str,
    declared_mime: str,
    size_bytes: int,
    sha256: str,
) -> bool:
    """True iff `provided_hmac` matches the recomputed manifest signature.

    Constant-time; never raises (False on any mismatch — the caller refuses the
    manifest: one-shot flag + CRITICAL + a security-flagged Review-Queue row, never
    screened, never extracted, never filed, and deliberately never result-posted so
    the bytes stay in D1 for forensics)."""
    expected = sign_manifest(
        secret,
        manifest_uuid=manifest_uuid,
        job_id=job_id,
        filename=filename,
        declared_mime=declared_mime,
        size_bytes=size_bytes,
        sha256=sha256,
    )
    return _hmac.compare_digest(expected, provided_hmac or "")
