"""§34 photo screening for Safety-Portal site-photo uploads (PR-2).

DETERMINISTIC, SEND-FREE, LLM-FREE. This module is the Mac-side trust boundary for
photos uploaded through the authenticated Safety Portal (Foundation Mission Invariant 2,
Layer 6 — attachment screening). Op Stds §34's photo pre-authorization (mission v4 §7,
"the four-sub-layer screening pipeline … applies before any Box upload or model call")
is enforced HERE, before `intake.py` lets a photo reach the PDF renderer or Box:

  L1  static signature checks   — magic-number (JPEG/PNG only) + decoded-size sanity.
  L2  structural inspection     — Pillow `verify()` + a decompression-bomb pixel cap +
                                  a forced re-encode to a fresh baseline JPEG. The
                                  re-encode is the load-bearing sanitizer: it destroys
                                  appended/polyglot payloads (anything after the codec's
                                  end-marker), strips ALL metadata (EXIF/GPS/ICC/XMP),
                                  and normalizes to one safe codec path.
  L3  ClamAV (optional)         — `clamd.scan_stream` on the ORIGINAL bytes, config-gated
                                  (`safety_reports.photo_screen.clamav_enabled`, default
                                  OFF). pyclamd is an OPTIONAL operator-installed dep,
                                  lazily imported; absent → ("ERROR", …) → suspicious.
  L4  VirusTotal                — explicitly skipped (Op Stds §34 Layer 4: "defer to
                                  Phase 2+"); not wired here.

Doctrine notes (see PR-2 brief + docs/tech_debt.md):
  * §34 Layer 2 was authored for PDF/Office attachments; it does NOT enumerate the
    image-specific threats (polyglot / appended payload / pixel bomb). The L2 measures
    here are best-practice for raster images, not a verbatim doctrine quote.
  * §34's malicious disposition ("ITS_Quarantine … sender DISABLED in
    ITS_Trusted_Contacts") has NO portal target — the portal has no inbound mailbox or
    allowlist. The portal adaptation (Review-Queue + CRITICAL page that NAMES the
    submitting account for operator disable) lives in `intake.py`; this module only
    classifies bytes and returns the verdict. Flagged for mission v4→v5 co-resolution.

This module performs NO network egress except the optional, config-gated clamd socket
(pyclamd, allowlisted in tests/test_capability_gating.py) and NO Smartsheet/Box I/O —
all disposition + filing is the caller's job. Kept pure so the security logic is unit-
testable without live infra.
"""
from __future__ import annotations

import base64
import binascii
import io
import logging
from dataclasses import dataclass
from typing import Literal

from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

Disposition = Literal["clean", "suspicious", "malicious"]

# The 4-key photo wire shape (mirror of safety_portal worker PHOTO_KEYS / the SPA
# PhotoValue interface). The Worker guarantees this shape on the happy path; we keep
# the tuple here for the defensive re-check at the trust boundary.
PHOTO_KEYS = ("data", "name", "taken_at", "gps")

# ── L1 — static signature constants ─────────────────────────────────────────────
# Accepted image magic numbers. Mirrors the Worker's JPEG/PNG gate; re-checked here
# because PR-2 is the real trust boundary (the Worker only sniffs the first 4 decoded
# bytes and could be bypassed by a direct internal-API call).
_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Decoded-bytes ceiling per photo. Mirrors the Worker's PHOTO_MAX_BYTES (400_000) as
# defense-in-depth: a photo reaching here larger than the Worker would have accepted
# means the Worker was bypassed → anomalous → suspicious.
MAX_DECODED_BYTES = 400_000

# Total photos screened per submission (mirrors the Worker's PHOTO_MAX_PER_SUBMISSION).
# A forged payload could carry more; the caller clamps to this and drops + logs extras.
MAX_PHOTOS_PER_SUBMISSION = 8

# ── L2 — structural / re-encode constants ───────────────────────────────────────
# Decompression-bomb pixel cap (width*height). Stricter than Pillow's own ~89.5MP
# DecompressionBombError default. A field photo is at most a few megapixels; 24MP is
# generous headroom while still refusing a pixel-bomb DoS.
MAX_IMAGE_PIXELS = 24_000_000

# Re-encode parameters for the forced clean-JPEG pass.
_REENCODE_QUALITY = 85
_MAX_DIMENSION = 2400  # clamp the longest side: bounds weekly-packet size, keeps detail

# Encoded base64 is ~4/3 the decoded size; cap the ENCODED length so decode_b64 refuses
# an oversize photo WITHOUT first materializing the decoded bytes (a forged internal-API
# submission could otherwise decode hundreds of MB before L1's size check rejects it).
_MAX_ENCODED_LEN = MAX_DECODED_BYTES * 4 // 3 + 8


class _DecompressionBombError(Exception):
    """Header pixel-count exceeds MAX_IMAGE_PIXELS — an active resource-exhaustion
    attack (malicious), distinct from a merely-unreadable image (suspicious)."""


@dataclass(frozen=True)
class PhotoScreenResult:
    """Verdict for one photo. `clean_jpeg` is the re-encoded, metadata-stripped,
    appended-payload-free JPEG and is set IFF `disposition == "clean"`."""

    disposition: Disposition
    clean_jpeg: bytes | None
    layer: str   # "L1" | "L2" | "L3" — which layer produced the verdict
    detail: str  # machine reason; NEVER contains image bytes or PII


def decode_b64(data: str) -> bytes | None:
    """Strict base64 decode (NO `data:` prefix, per the wire contract). None on failure.

    `validate=True` rejects any non-base64 character — the Worker already constrains the
    charset, but this is the trust boundary so the decode is re-validated here. An
    over-long encoding is refused BEFORE decoding so an oversize/forged photo never
    materializes in memory (the decoded bytes are also size-checked at L1).
    """
    if not data or len(data) > _MAX_ENCODED_LEN:
        return None
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return None


def _magic_ok(raw: bytes) -> bool:
    return raw.startswith(_JPEG_MAGIC) or raw.startswith(_PNG_MAGIC)


def _verify_and_reencode(raw: bytes) -> bytes:
    """L2: structural verify + decompression-bomb cap + forced re-encode to clean JPEG.

    Raises `_DecompressionBombError` on a pixel-bomb (caller → malicious) and the usual
    Pillow decode errors on a structurally-bad image (caller → suspicious). On success
    returns a fresh baseline JPEG with all metadata stripped and any trailing/appended
    bytes destroyed (only the decoded pixels survive the re-encode).
    """
    # Header pass: read dimensions WITHOUT decoding pixels, then verify integrity.
    with Image.open(io.BytesIO(raw)) as probe:
        width, height = probe.size
        if width * height > MAX_IMAGE_PIXELS:
            raise _DecompressionBombError(f"{width}x{height}")
        probe.verify()  # detects truncation / CRC corruption; leaves image unusable
    # Re-open for the actual decode + re-encode (verify() consumed the probe).
    with Image.open(io.BytesIO(raw)) as img:
        img.load()                       # force full decode (raises on hidden truncation)
        rgb = img.convert("RGB")         # flatten palette/alpha → baseline-JPEG-safe
        rgb.thumbnail((_MAX_DIMENSION, _MAX_DIMENSION))  # clamp longest side, keep ratio
        # Rebuild from the raw pixel buffer so NO source metadata survives into the
        # output. open()/convert()/thumbnail() carry `.info` forward (EXIF, ICC, and
        # crucially the JPEG COM *comment* marker — a payload-bearing field that Pillow
        # RE-EMITS on save). A fresh frombytes() image has an empty `.info`, so the
        # re-encoded JPEG carries decoded pixels only — the "strips ALL metadata" contract.
        clean = Image.frombytes(rgb.mode, rgb.size, rgb.tobytes())
        out = io.BytesIO()
        clean.save(out, format="JPEG", quality=_REENCODE_QUALITY, optimize=True)
        return out.getvalue()


def _clamav_scan(raw: bytes) -> tuple[str, str | None]:
    """L3: stream the ORIGINAL bytes to the local clamd daemon via pyclamd.

    Returns one of ("OK", None) | ("FOUND", signature) | ("ERROR", detail). NEVER raises:
    a missing dep / unreachable daemon / unexpected result all degrade to ("ERROR", …)
    so the caller routes the photo to review rather than crashing the submission.

    pyclamd is lazily imported — it is an OPTIONAL, operator-installed dependency only
    needed when `clamav_enabled`. The original (pre-re-encode) bytes are scanned because
    the re-encode would strip any embedded signature before clamd could see it.
    """
    try:
        import pyclamd  # noqa: PLC0415 — lazy + optional; operator-installed when clamav on
    except ImportError:
        return ("ERROR", "pyclamd_unavailable")
    try:
        cd = pyclamd.ClamdUnixSocket()
        result = cd.scan_stream(raw)  # None if clean; {"stream": ("FOUND", sig)} on a hit
    except Exception as exc:  # noqa: BLE001 — any clamd/socket failure → ERROR, never crash
        return ("ERROR", type(exc).__name__)
    if not result:
        return ("OK", None)
    status = result.get("stream") if isinstance(result, dict) else None
    if isinstance(status, (tuple, list)) and status and status[0] == "FOUND":
        return ("FOUND", str(status[1]) if len(status) > 1 else "unknown")
    return ("ERROR", "unexpected_clamd_result")


def screen_photo(raw: bytes, *, clamav_enabled: bool = False) -> PhotoScreenResult:
    """Run the §34 sub-layers on ONE decoded photo and return the verdict.

    Layers short-circuit cost-ordered (L1 cheap → L2 → L3). A `clean` result carries the
    re-encoded `clean_jpeg`; `suspicious` / `malicious` carry None (the bytes are refused,
    never filed or embedded). Disposition ladder:
      * malicious  — active attack: decompression bomb (L2) or ClamAV signature (L3).
      * suspicious — refused-but-unproven: empty / oversize / wrong-magic (L1),
                     structurally unreadable (L2), or clamd required-but-unavailable (L3).
      * clean      — survived every enabled layer; `clean_jpeg` is safe to embed/upload.
    """
    # L1 — static signature checks.
    if not raw:
        return PhotoScreenResult("suspicious", None, "L1", "empty")
    if len(raw) > MAX_DECODED_BYTES:
        return PhotoScreenResult("suspicious", None, "L1", f"oversize:{len(raw)}")
    if not _magic_ok(raw):
        return PhotoScreenResult("suspicious", None, "L1", "magic_mismatch")

    # L2 — structural inspection + bomb cap + forced re-encode.
    try:
        clean = _verify_and_reencode(raw)
    except _DecompressionBombError as exc:
        return PhotoScreenResult("malicious", None, "L2", f"decompression_bomb:{exc}")
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        # Pillow: OSError on truncation, SyntaxError on some malformed headers,
        # UnidentifiedImageError on non-images. Structurally bad ≠ proven malicious.
        return PhotoScreenResult("suspicious", None, "L2", f"unreadable:{type(exc).__name__}")

    # L3 — ClamAV (optional, config-gated). Scans the ORIGINAL bytes.
    if clamav_enabled:
        verdict, sig = _clamav_scan(raw)
        if verdict == "FOUND":
            return PhotoScreenResult("malicious", None, "L3", f"clamav:{sig}")
        if verdict == "ERROR":
            # Scanner required but unavailable → do NOT pass blindly; route to review.
            return PhotoScreenResult("suspicious", None, "L3", f"clamav_unavailable:{sig}")

    return PhotoScreenResult("clean", clean, "L3" if clamav_enabled else "L2", "ok")


def iter_photo_fields(definition: dict) -> list[tuple[str, str, int]]:
    """Return the header-level photo fields of a form definition as
    `(key, label, max_count)`. Photos are HEADER-level only (meta-schema: enforced by
    publishValidation); repeating-table / checklist fields are never scanned. `max_count`
    is clamped to the legal 1..4 range, defaulting to 4 when absent/invalid.
    """
    out: list[tuple[str, str, int]] = []
    for section in definition.get("sections", []) or []:
        if not isinstance(section, dict) or section.get("type") != "header":
            continue
        for f in section.get("fields", []) or []:
            if isinstance(f, dict) and f.get("input") == "photo":
                mc = f.get("max_count")
                max_count = mc if isinstance(mc, int) and 1 <= mc <= 4 else 4
                out.append((str(f.get("key", "")), str(f.get("label", "")), max_count))
    return out


THUMB_LONG_EDGE = 320
THUMB_JPEG_QUALITY = 70


def make_thumbnail(clean_jpeg: bytes) -> bytes | None:
    """Small picker thumbnail (≤~25KB JPEG) from the §34 CLEAN RE-ENCODE — NEVER the raw
    upload (a thumbnail of unscreened bytes would be serving the attacker's file at small
    size). Rides the daily-photo result post / site-photo register so the WPR screen can
    show an image instead of a blind date+caption pick (0074; operator-approved relaxation
    of the record-only posture, thumbnails only).

    Fenced: ANY failure returns None — a thumb is optional display metadata, and its
    absence degrades to the thumbless card, never an error or a blocked disposition."""
    try:
        img = Image.open(io.BytesIO(clean_jpeg))
        img.thumbnail((THUMB_LONG_EDGE, THUMB_LONG_EDGE))
        normalized = img.convert("RGB") if img.mode not in ("RGB", "L") else img
        out = io.BytesIO()
        normalized.save(out, format="JPEG", quality=THUMB_JPEG_QUALITY)
        return out.getvalue()
    except Exception:  # noqa: BLE001 — optional metadata; never let a thumb sink a disposition
        return None


def build_caption(name: str, taken_at: str, gps: str) -> str:
    """Build a display caption from the UNTRUSTED EXIF sidecar (mirrors the SPA's join:
    `[taken_at(T→space), gps]` plus the original filename). Pure string — the caller MUST
    escape it before placing it in a PDF/HTML (these are attacker-influenced values)."""
    taken = taken_at.replace("T", " ").strip() if taken_at else ""
    parts = [p for p in (name.strip() if name else "", taken, gps.strip() if gps else "") if p]
    return " · ".join(parts)
