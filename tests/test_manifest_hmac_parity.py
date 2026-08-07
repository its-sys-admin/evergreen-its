"""Golden-vector parity tests for the manifest:v1 materials-manifest HMAC protocol.

The pinned contract (PR3b): the Worker signs each uploaded materials manifest — a
BOM or a shipping log — at upload time over the domain-separated canonical string

    "manifest:v1" \\n manifest_uuid \\n job_id \\n filename \\n declared_mime
                  \\n str(size_bytes) \\n sha256_hex

with HMAC-SHA256 → lowercase hex (the portal HMAC secret — Worker env
HMAC_PAYLOAD_SECRET / Keychain ITS_PORTAL_HMAC_SECRET, SHARED by every protocol;
isolation comes from the leading domain literal, never from key separation). The
Mac side (`shared.portal_hmac.manifest_canonical` + `verify_manifest`) recomputes
it constant-time before a single byte is §34-screened, extracted or filed — the
po-att:v1 / est:v1 content-covered pattern with materials-manifest identity
(`manifest_uuid` + `job_id`) in place of estimate identity (`est_uuid` + `job_no`).

Binding to `job_id` (not just the row uuid) is load-bearing HERE in a way it is not
in the estimate lane: the manifest pool's dedupe is deliberately PER-JOB, because one
master BOM legitimately serves sibling jobs (Bradley 1 / Bradley 2). The same bytes
therefore exist under two `job_id`s by design, and only the signature keeps a row
signed for one job from being replayed onto the other.

Every expected signature here is computed IN THE TEST from the pinned canonical
string with stdlib hmac/hashlib — independent of the implementation under test,
so a drifted canonical (reordered fields, wrong separator, wrong domain, missing
utf-8 encode) fails against the golden math, not against itself.

Run with: pytest -q tests/test_manifest_hmac_parity.py
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
from typing import Any

from shared import portal_hmac

SECRET = "manifest-parity-test-secret"

# One fixed golden vector (realistic corpus-shaped values — the Evergreen manifest
# corpus is full of parenthesized revision-chain filenames like "… (1).pdf").
FIELDS: dict[str, Any] = {
    "manifest_uuid": "3b71f8c0-52ad-4e19-9c7b-6d04a1e83f27",
    "job_id": "JOB-2026-014",
    "filename": "25-35099 - EVERGREEN ENERGY - BONACCI 1 - DELTA BOM (1).pdf",
    "declared_mime": "application/pdf",
    "size_bytes": 148_902,
    "sha256": "4e07408562bedb8b60ce05c1decfe3ad16b72230967de01f640b7e4729b49fce",
}

# A second vector with a non-ASCII filename — pins the utf-8 encode of the
# canonical string (a latin-1 or ascii-errors encode would diverge here).
FIELDS_UNICODE: dict[str, Any] = {
    **FIELDS,
    "manifest_uuid": "c41d0aa9-2222-4e19-9c7b-6d04a1e83f88",
    "filename": "Röckwool — Bordereau n°7 (naïve).xlsx",
    "declared_mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

MANIFEST_DOMAIN = "manifest:v1"
EST_DOMAIN = "est:v1"


def _canonical(domain: str, f: dict[str, Any]) -> str:
    """The pinned wire string, built HERE from the contract — not via portal_hmac."""
    return "\n".join([
        domain,
        f["manifest_uuid"],
        f["job_id"],
        f["filename"],
        f["declared_mime"],
        str(f["size_bytes"]),
        f["sha256"],
    ])


def _hmac_hex(secret: str, message: str) -> str:
    return _hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def _golden_sig(f: dict[str, Any]) -> str:
    return _hmac_hex(SECRET, _canonical(MANIFEST_DOMAIN, f))


# ---- canonical-string parity -------------------------------------------------------


def test_manifest_canonical_builds_the_exact_pinned_string():
    assert portal_hmac.manifest_canonical(**FIELDS) == _canonical(MANIFEST_DOMAIN, FIELDS)


def test_manifest_canonical_pinned_literal():
    """Fully-literal golden string — immune to a bug shared by test helper and impl."""
    expected = (
        "manifest:v1\n"
        "3b71f8c0-52ad-4e19-9c7b-6d04a1e83f27\n"
        "JOB-2026-014\n"
        "25-35099 - EVERGREEN ENERGY - BONACCI 1 - DELTA BOM (1).pdf\n"
        "application/pdf\n"
        "148902\n"
        "4e07408562bedb8b60ce05c1decfe3ad16b72230967de01f640b7e4729b49fce"
    )
    assert portal_hmac.manifest_canonical(**FIELDS) == expected


def test_manifest_canonical_size_bytes_is_decimal_str():
    """size_bytes rides as str(int) — no float repr, no padding, no separators.

    The Worker renders it with String(sizeBytes); that equivalence holds for ints
    ONLY, which is why the daemon coerces a non-int to -1 and fails closed.
    """
    got = portal_hmac.manifest_canonical(**{**FIELDS, "size_bytes": 7})
    assert "\n7\n" in got


def test_manifest_canonical_domain_is_the_first_line():
    """The domain literal leads — that placement IS the protocol separation."""
    assert portal_hmac.manifest_canonical(**FIELDS).split("\n")[0] == "manifest:v1"
    assert portal_hmac.MANIFEST_DOMAIN == "manifest:v1"


# ---- accept path -------------------------------------------------------------------


def test_verify_accepts_the_golden_vector():
    assert portal_hmac.verify_manifest(SECRET, _golden_sig(FIELDS), **FIELDS) is True


def test_verify_accepts_unicode_filename_vector():
    assert (
        portal_hmac.verify_manifest(SECRET, _golden_sig(FIELDS_UNICODE), **FIELDS_UNICODE) is True
    )


# ---- reject paths (each mutation MUST break verification) --------------------------


def test_flipped_byte_in_sha256_rejected():
    """The signature covers the content digest — one flipped hex nibble kills it."""
    sig = _golden_sig(FIELDS)
    bad_sha = ("a" if FIELDS["sha256"][0] != "a" else "b") + FIELDS["sha256"][1:]
    assert bad_sha != FIELDS["sha256"]
    tampered = {**FIELDS, "sha256": bad_sha}
    assert portal_hmac.verify_manifest(SECRET, sig, **tampered) is False


def test_signature_over_different_filename_rejected():
    """Simulated field swap/tamper: a signature minted for one filename must not
    verify against another (rename-after-signing, the po-att posture)."""
    other = {**FIELDS, "filename": "renamed-after-signing.pdf"}
    sig_for_other = _golden_sig(other)
    assert portal_hmac.verify_manifest(SECRET, sig_for_other, **FIELDS) is False


def test_signature_for_one_job_never_verifies_for_a_sibling_job():
    """THE per-job-dedupe consequence: a master BOM legitimately uploads to two
    sibling jobs, so identical bytes exist under two job_ids. A row signed for
    Bradley 1 must not verify as Bradley 2 — the signature is the only thing
    stopping a cross-job replay of byte-identical content."""
    sibling = {**FIELDS, "job_id": "JOB-2026-015"}
    sig_for_sibling = _golden_sig(sibling)
    assert sig_for_sibling != _golden_sig(FIELDS)
    assert portal_hmac.verify_manifest(SECRET, sig_for_sibling, **FIELDS) is False


def test_swapped_field_order_rejected():
    """manifest_uuid and job_id exchanged between slots — same bytes, wrong positions.
    Proves the canonical is position-bound, not a bag of values."""
    swapped = {**FIELDS, "manifest_uuid": FIELDS["job_id"], "job_id": FIELDS["manifest_uuid"]}
    sig_for_swapped = _hmac_hex(SECRET, _canonical(MANIFEST_DOMAIN, swapped))
    assert portal_hmac.verify_manifest(SECRET, sig_for_swapped, **FIELDS) is False


def test_est_domain_signature_never_verifies_as_manifest():
    """DOMAIN-SEPARATION PROOF: an HMAC minted under the est:v1 domain over the
    IDENTICAL field tail must not verify under manifest:v1 — cross-protocol
    signature confusion is structurally impossible (the Invariant-2 requirement).
    The two protocols share an identical 7-field flat shape, so the leading domain
    literal is the ONLY thing separating them."""
    est_sig = _hmac_hex(SECRET, _canonical(EST_DOMAIN, FIELDS))
    assert est_sig != _golden_sig(FIELDS)  # sanity: the domains really diverge
    assert portal_hmac.verify_manifest(SECRET, est_sig, **FIELDS) is False


def test_real_estimate_signature_never_verifies_as_manifest():
    """Same proof via the LIVE est:v1 signer (not a hand-built string): sign a vendor
    estimate sharing uuid/identity/filename/mime/size/sha, then try it as a manifest."""
    est_sig = portal_hmac.sign_po_estimate(
        SECRET,
        est_uuid=FIELDS["manifest_uuid"],
        job_no=FIELDS["job_id"],
        filename=FIELDS["filename"],
        declared_mime=FIELDS["declared_mime"],
        size_bytes=FIELDS["size_bytes"],
        sha256=FIELDS["sha256"],
    )
    assert portal_hmac.verify_manifest(SECRET, est_sig, **FIELDS) is False


def test_real_manifest_signature_never_verifies_as_estimate():
    """The converse direction — a manifest signature must not open the estimate lane
    either. Both directions are asserted because domain separation that holds one way
    and not the other is not separation."""
    manifest_sig = portal_hmac.sign_manifest(SECRET, **FIELDS)
    assert (
        portal_hmac.verify_po_estimate(
            SECRET,
            manifest_sig,
            est_uuid=FIELDS["manifest_uuid"],
            job_no=FIELDS["job_id"],
            filename=FIELDS["filename"],
            declared_mime=FIELDS["declared_mime"],
            size_bytes=FIELDS["size_bytes"],
            sha256=FIELDS["sha256"],
        )
        is False
    )


def test_wrong_secret_rejected():
    sig = _golden_sig(FIELDS)
    assert portal_hmac.verify_manifest("other-secret", sig, **FIELDS) is False


def test_absent_or_empty_signature_rejected_without_raising():
    """The verify contract: never raises — False on any mismatch incl. None/empty
    (the fail-closed downgrade defense the daemon relies on)."""
    assert portal_hmac.verify_manifest(SECRET, None, **FIELDS) is False
    assert portal_hmac.verify_manifest(SECRET, "", **FIELDS) is False
    assert portal_hmac.verify_manifest(SECRET, "not-hex-at-all", **FIELDS) is False
