"""Golden-vector parity tests for the schedule:v1 job-schedule HMAC protocol.

The pinned contract (ADR-0006): the Worker signs each uploaded project schedule —
a Smartsheet Gantt PDF export — at upload time over the domain-separated canonical
string

    "schedule:v1" \\n schedule_uuid \\n job_id \\n filename \\n declared_mime
                  \\n str(size_bytes) \\n sha256_hex

with HMAC-SHA256 → lowercase hex (the portal HMAC secret — Worker env
HMAC_PAYLOAD_SECRET / Keychain ITS_PORTAL_HMAC_SECRET, SHARED by every protocol;
isolation comes from the leading domain literal, never from key separation). The
Mac side (`shared.portal_hmac.schedule_canonical` + `verify_schedule`) recomputes
it constant-time before a single byte is §34-screened, rendered, OCR'd or filed.

The NEAREST SIBLING is manifest:v1 — the two protocols share an IDENTICAL 7-field
flat shape, so the leading domain literal is the ONLY thing separating them. Both
cross-directions are asserted below, against hand-built strings AND against the
live sibling signer, because domain separation that holds one way and not the
other is not separation.

Binding to `job_id` matters here for the lane's own reason: a SUPERSEDED
revision's exact bytes may legally re-enter under the same job (the rollback path
for a wrong-file commit), so byte-identical documents recur by design and only
the signature stops a row signed for one job being replayed onto another.

Every expected signature here is computed IN THE TEST from the pinned canonical
string with stdlib hmac/hashlib — independent of the implementation under test,
so a drifted canonical (reordered fields, wrong separator, wrong domain, missing
utf-8 encode) fails against the golden math, not against itself.

Run with: pytest -q tests/test_schedule_hmac_parity.py
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
from typing import Any

from shared import portal_hmac

SECRET = "schedule-parity-test-secret"

# One fixed golden vector (realistic corpus-shaped values — the schedule corpus is
# "Project Schedule - <Client> - <Job> <date>.pdf" Smartsheet exports).
FIELDS: dict[str, Any] = {
    "schedule_uuid": "9d24af71-88c3-4b02-b615-2f7a90cd41e6",
    "job_id": "JOB-2026-021",
    "filename": "Project Schedule - KSI - Coker 8.5.26.pdf",
    "declared_mime": "application/pdf",
    "size_bytes": 724_954,
    "sha256": "2c26b46b68ffc68ff99b453c1d30413413422d706483bfa0f98a5e886266e7ae",
}

# A second vector with a non-ASCII filename — pins the utf-8 encode of the
# canonical string (a latin-1 or ascii-errors encode would diverge here).
FIELDS_UNICODE: dict[str, Any] = {
    **FIELDS,
    "schedule_uuid": "5f81c2d4-aaaa-4b02-b615-2f7a90cd41ff",
    "filename": "Échéancier — Projet n°3 (révisé).pdf",
}

SCHEDULE_DOMAIN = "schedule:v1"
MANIFEST_DOMAIN = "manifest:v1"


def _canonical(domain: str, f: dict[str, Any]) -> str:
    """The pinned wire string, built HERE from the contract — not via portal_hmac."""
    return "\n".join([
        domain,
        f["schedule_uuid"],
        f["job_id"],
        f["filename"],
        f["declared_mime"],
        str(f["size_bytes"]),
        f["sha256"],
    ])


def _hmac_hex(secret: str, message: str) -> str:
    return _hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def _golden_sig(f: dict[str, Any]) -> str:
    return _hmac_hex(SECRET, _canonical(SCHEDULE_DOMAIN, f))


# ---- canonical-string parity -------------------------------------------------------


def test_schedule_canonical_builds_the_exact_pinned_string():
    assert portal_hmac.schedule_canonical(**FIELDS) == _canonical(SCHEDULE_DOMAIN, FIELDS)


def test_schedule_canonical_pinned_literal():
    """Fully-literal golden string — immune to a bug shared by test helper and impl."""
    expected = (
        "schedule:v1\n"
        "9d24af71-88c3-4b02-b615-2f7a90cd41e6\n"
        "JOB-2026-021\n"
        "Project Schedule - KSI - Coker 8.5.26.pdf\n"
        "application/pdf\n"
        "724954\n"
        "2c26b46b68ffc68ff99b453c1d30413413422d706483bfa0f98a5e886266e7ae"
    )
    assert portal_hmac.schedule_canonical(**FIELDS) == expected


def test_schedule_canonical_size_bytes_is_decimal_str():
    """size_bytes rides as str(int) — no float repr, no padding, no separators.

    The Worker renders it with String(sizeBytes); that equivalence holds for ints
    ONLY, which is why the daemon must coerce a non-int to -1 and fail closed.
    """
    got = portal_hmac.schedule_canonical(**{**FIELDS, "size_bytes": 7})
    assert "\n7\n" in got


def test_schedule_canonical_domain_is_the_first_line():
    """The domain literal leads — that placement IS the protocol separation."""
    assert portal_hmac.schedule_canonical(**FIELDS).split("\n")[0] == "schedule:v1"
    assert portal_hmac.SCHEDULE_DOMAIN == "schedule:v1"


# ---- accept path -------------------------------------------------------------------


def test_verify_accepts_the_golden_vector():
    assert portal_hmac.verify_schedule(SECRET, _golden_sig(FIELDS), **FIELDS) is True


def test_verify_accepts_unicode_filename_vector():
    assert (
        portal_hmac.verify_schedule(SECRET, _golden_sig(FIELDS_UNICODE), **FIELDS_UNICODE) is True
    )


# ---- reject paths (each mutation MUST break verification) --------------------------


def test_flipped_byte_in_sha256_rejected():
    """The signature covers the content digest — one flipped hex nibble kills it."""
    sig = _golden_sig(FIELDS)
    bad_sha = ("a" if FIELDS["sha256"][0] != "a" else "b") + FIELDS["sha256"][1:]
    assert bad_sha != FIELDS["sha256"]
    tampered = {**FIELDS, "sha256": bad_sha}
    assert portal_hmac.verify_schedule(SECRET, sig, **tampered) is False


def test_signature_over_different_filename_rejected():
    """Simulated field swap/tamper: a signature minted for one filename must not
    verify against another (rename-after-signing, the po-att posture)."""
    other = {**FIELDS, "filename": "renamed-after-signing.pdf"}
    sig_for_other = _golden_sig(other)
    assert portal_hmac.verify_schedule(SECRET, sig_for_other, **FIELDS) is False


def test_signature_for_one_job_never_verifies_for_a_sibling_job():
    """A row signed for one job must not verify as another — load-bearing here
    because a superseded revision's exact bytes legally recur under the SAME job
    (the rollback path), so byte-identical content exists across time and must
    stay pinned to its job."""
    sibling = {**FIELDS, "job_id": "JOB-2026-022"}
    sig_for_sibling = _golden_sig(sibling)
    assert sig_for_sibling != _golden_sig(FIELDS)
    assert portal_hmac.verify_schedule(SECRET, sig_for_sibling, **FIELDS) is False


def test_swapped_field_order_rejected():
    """schedule_uuid and job_id exchanged between slots — same bytes, wrong positions.
    Proves the canonical is position-bound, not a bag of values."""
    swapped = {**FIELDS, "schedule_uuid": FIELDS["job_id"], "job_id": FIELDS["schedule_uuid"]}
    sig_for_swapped = _hmac_hex(SECRET, _canonical(SCHEDULE_DOMAIN, swapped))
    assert portal_hmac.verify_schedule(SECRET, sig_for_swapped, **FIELDS) is False


def test_manifest_domain_signature_never_verifies_as_schedule():
    """DOMAIN-SEPARATION PROOF against the NEAREST sibling: an HMAC minted under the
    manifest:v1 domain over the IDENTICAL field tail must not verify under
    schedule:v1. The two protocols share an identical 7-field flat shape, so the
    leading domain literal is the ONLY thing separating them."""
    manifest_sig = _hmac_hex(SECRET, _canonical(MANIFEST_DOMAIN, FIELDS))
    assert manifest_sig != _golden_sig(FIELDS)  # sanity: the domains really diverge
    assert portal_hmac.verify_schedule(SECRET, manifest_sig, **FIELDS) is False


def test_real_manifest_signature_never_verifies_as_schedule():
    """Same proof via the LIVE manifest:v1 signer (not a hand-built string): sign a
    manifest sharing uuid/job/filename/mime/size/sha, then try it as a schedule."""
    manifest_sig = portal_hmac.sign_manifest(
        SECRET,
        manifest_uuid=FIELDS["schedule_uuid"],
        job_id=FIELDS["job_id"],
        filename=FIELDS["filename"],
        declared_mime=FIELDS["declared_mime"],
        size_bytes=FIELDS["size_bytes"],
        sha256=FIELDS["sha256"],
    )
    assert portal_hmac.verify_schedule(SECRET, manifest_sig, **FIELDS) is False


def test_real_schedule_signature_never_verifies_as_manifest():
    """The converse direction — a schedule signature must not open the manifest lane
    either. Both directions are asserted because domain separation that holds one way
    and not the other is not separation."""
    schedule_sig = portal_hmac.sign_schedule(SECRET, **FIELDS)
    assert (
        portal_hmac.verify_manifest(
            SECRET,
            schedule_sig,
            manifest_uuid=FIELDS["schedule_uuid"],
            job_id=FIELDS["job_id"],
            filename=FIELDS["filename"],
            declared_mime=FIELDS["declared_mime"],
            size_bytes=FIELDS["size_bytes"],
            sha256=FIELDS["sha256"],
        )
        is False
    )


def test_wrong_secret_rejected():
    sig = _golden_sig(FIELDS)
    assert portal_hmac.verify_schedule("other-secret", sig, **FIELDS) is False


def test_absent_or_empty_signature_rejected_without_raising():
    """The verify contract: never raises — False on any mismatch incl. None/empty
    (the fail-closed downgrade defense the daemon relies on)."""
    assert portal_hmac.verify_schedule(SECRET, None, **FIELDS) is False
    assert portal_hmac.verify_schedule(SECRET, "", **FIELDS) is False
    assert portal_hmac.verify_schedule(SECRET, "not-hex-at-all", **FIELDS) is False
