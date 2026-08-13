"""Unit tests for safety_reports/photo_screen.py — the §34 photo trust boundary.

Pure/deterministic: builds real JPEG/PNG fixtures with Pillow, exercises every layer
verdict (L1 magic/size, L2 verify/re-encode/decompression-bomb, L3 ClamAV tristate), and
locks the disposition ladder (clean / suspicious / malicious). ClamAV is exercised by
patching `_clamav_scan` so no live clamd daemon (and no pyclamd install) is required —
the EICAR string is the canonical "FOUND" stand-in per Op Stds §34.
"""
from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from safety_reports import photo_screen

# EICAR test signature — the canonical, harmless malware-detection probe (§34). Used
# here only as a label in the mocked clamd FOUND result; the bytes are never scanned live.
EICAR = (
    r"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
)


# ── fixtures ────────────────────────────────────────────────────────────────────
def _jpeg(size: tuple[int, int] = (64, 48), color: tuple[int, int, int] = (10, 120, 60)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _png(size: tuple[int, int] = (64, 48), color: tuple[int, int, int] = (200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_with_exif() -> bytes:
    """A JPEG carrying an EXIF block (so we can assert the re-encode strips it)."""
    img = Image.new("RGB", (32, 32), (5, 5, 5))
    exif = Image.Exif()
    exif[0x010F] = "EVERGREEN-CAM"   # Make
    exif[0x9003] = "2026:06:12 09:30:00"  # DateTimeOriginal
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


# ── L1: static signature checks ───────────────────────────────────────────────
def test_clean_jpeg_passes_and_reencodes():
    res = photo_screen.screen_photo(_jpeg())
    assert res.disposition == "clean"
    assert res.clean_jpeg is not None
    # The clean output is itself a valid, re-openable JPEG.
    out = Image.open(io.BytesIO(res.clean_jpeg))
    assert out.format == "JPEG"


def test_clean_png_reencoded_to_jpeg():
    res = photo_screen.screen_photo(_png())
    assert res.disposition == "clean"
    assert res.clean_jpeg is not None
    assert res.clean_jpeg.startswith(b"\xff\xd8\xff")  # output is JPEG regardless of input format


def test_empty_is_suspicious():
    res = photo_screen.screen_photo(b"")
    assert res.disposition == "suspicious"
    assert res.layer == "L1"
    assert res.detail == "empty"
    assert res.clean_jpeg is None


def test_wrong_magic_is_suspicious():
    res = photo_screen.screen_photo(b"GIF89a" + b"\x00" * 32)
    assert res.disposition == "suspicious"
    assert res.layer == "L1"
    assert res.detail == "magic_mismatch"


def test_oversize_is_suspicious():
    # Valid JPEG magic but past the decoded-bytes ceiling → defense-in-depth refusal.
    raw = b"\xff\xd8\xff" + b"\x00" * photo_screen.MAX_DECODED_BYTES
    res = photo_screen.screen_photo(raw)
    assert res.disposition == "suspicious"
    assert res.layer == "L1"
    assert res.detail.startswith("oversize:")


# ── L2: structural inspection + re-encode + bomb cap ─────────────────────────
def test_truncated_jpeg_is_suspicious():
    raw = _jpeg((128, 96))[:64]  # JPEG header intact, body truncated
    res = photo_screen.screen_photo(raw)
    assert res.disposition == "suspicious"
    assert res.layer == "L2"
    assert res.detail.startswith("unreadable:")


def test_decompression_bomb_is_malicious():
    # Solid-color PNG with dimensions exceeding MAX_IMAGE_PIXELS but a tiny encoded size
    # (the classic pixel-bomb) — header dims trip the cap before any pixel decode.
    bomb = _png((5001, 5001))  # 25_010_001 px > 24_000_000 cap; solid color ⇒ small file
    assert len(bomb) <= photo_screen.MAX_DECODED_BYTES  # passes L1 size, reaches L2
    res = photo_screen.screen_photo(bomb)
    assert res.disposition == "malicious"
    assert res.layer == "L2"
    assert res.detail.startswith("decompression_bomb:")


def test_reencode_strips_exif():
    src = _jpeg_with_exif()
    assert Image.open(io.BytesIO(src)).getexif()  # source HAS exif
    res = photo_screen.screen_photo(src)
    assert res.disposition == "clean"
    assert res.clean_jpeg is not None
    # The re-encoded output carries no EXIF tags (the "strip" half of caption-then-strip).
    assert dict(Image.open(io.BytesIO(res.clean_jpeg)).getexif()) == {}


def test_reencode_destroys_appended_payload():
    # A JPEG with bytes appended after EOI (polyglot/appended-payload shape). It still
    # decodes as a valid image (Pillow ignores the trailing bytes), so the verdict is
    # clean — but the clean_jpeg is a fresh re-encode that does NOT contain the payload.
    payload = b"<?php system($_GET['c']); ?>" * 8
    raw = _jpeg() + payload
    res = photo_screen.screen_photo(raw)
    assert res.disposition == "clean"
    assert res.clean_jpeg is not None
    assert payload not in res.clean_jpeg


def test_reencode_strips_jpeg_comment_marker():
    # A payload smuggled in a JPEG COM (comment) marker — Pillow RE-EMITS info['comment']
    # on save, so a naive re-encode would carry it through. The frombytes() rebuild drops
    # all source .info, so the payload must be absent from the clean output.
    payload = b"PAYLOAD-IN-COM-MARKER-system($_GET)"
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), (7, 7, 7)).save(buf, format="JPEG", comment=payload)
    src = buf.getvalue()
    assert payload in src  # the source JPEG genuinely carries the payload in its COM marker
    res = photo_screen.screen_photo(src)
    assert res.disposition == "clean"
    assert res.clean_jpeg is not None
    assert payload not in res.clean_jpeg


# ── L3: ClamAV (mocked) ──────────────────────────────────────────────────────
def test_clamav_disabled_does_not_scan(mocker):
    spy = mocker.patch("safety_reports.photo_screen._clamav_scan")
    res = photo_screen.screen_photo(_jpeg(), clamav_enabled=False)
    assert res.disposition == "clean"
    spy.assert_not_called()


def test_clamav_found_is_malicious(mocker):
    mocker.patch(
        "safety_reports.photo_screen._clamav_scan",
        return_value=("FOUND", "Eicar-Test-Signature"),
    )
    res = photo_screen.screen_photo(_jpeg(), clamav_enabled=True)
    assert res.disposition == "malicious"
    assert res.layer == "L3"
    assert "Eicar-Test-Signature" in res.detail
    assert res.clean_jpeg is None


def test_clamav_error_is_suspicious(mocker):
    mocker.patch(
        "safety_reports.photo_screen._clamav_scan",
        return_value=("ERROR", "pyclamd_unavailable"),
    )
    res = photo_screen.screen_photo(_jpeg(), clamav_enabled=True)
    assert res.disposition == "suspicious"
    assert res.layer == "L3"
    assert res.detail.startswith("clamav_unavailable")


def test_clamav_ok_is_clean(mocker):
    scan = mocker.patch(
        "safety_reports.photo_screen._clamav_scan", return_value=("OK", None)
    )
    res = photo_screen.screen_photo(_jpeg(), clamav_enabled=True)
    assert res.disposition == "clean"
    assert res.clean_jpeg is not None
    scan.assert_called_once()


def test_clamav_scans_original_not_reencoded(mocker):
    # L3 must scan the ORIGINAL upload bytes, not the re-encoded output.
    src = _jpeg((40, 40))
    scan = mocker.patch(
        "safety_reports.photo_screen._clamav_scan", return_value=("OK", None)
    )
    photo_screen.screen_photo(src, clamav_enabled=True)
    scanned = scan.call_args.args[0]
    assert scanned == src


def test_clamav_scan_unavailable_pyclamd(mocker):
    # The real _clamav_scan returns ERROR (never raises) when pyclamd can't be imported.
    mocker.patch.dict("sys.modules", {"pyclamd": None})  # force ImportError on `import pyclamd`
    verdict, detail = photo_screen._clamav_scan(b"abc")
    assert verdict == "ERROR"
    assert detail == "pyclamd_unavailable"


# ── decode_b64 ───────────────────────────────────────────────────────────────
def test_decode_b64_roundtrip():
    raw = _jpeg()
    assert photo_screen.decode_b64(base64.b64encode(raw).decode()) == raw


def test_decode_b64_rejects_garbage():
    assert photo_screen.decode_b64("not base64 @@@") is None
    assert photo_screen.decode_b64("") is None


def test_decode_b64_rejects_data_uri_prefix():
    # The wire contract carries NO `data:` prefix; the colon/semicolon are non-base64.
    assert photo_screen.decode_b64("data:image/jpeg;base64,AAAA") is None


def test_decode_b64_rejects_oversize_without_decoding():
    # A valid-charset base64 string past the encoded ceiling is refused BEFORE decode, so
    # an oversize/forged photo never materializes in memory.
    huge = "A" * (photo_screen._MAX_ENCODED_LEN + 4)
    assert photo_screen.decode_b64(huge) is None


# ── iter_photo_fields ────────────────────────────────────────────────────────
def test_iter_photo_fields_header_only():
    definition = {
        "sections": [
            {
                "type": "header",
                "fields": [
                    {"key": "job", "input": "text", "label": "Job"},
                    {"key": "site_photos", "input": "photo", "label": "Site Photos", "max_count": 3},
                ],
            },
            # A non-header section with a (illegal) photo input must be ignored.
            {"type": "repeating_table", "columns": [{"key": "p", "input": "photo"}]},
        ]
    }
    assert photo_screen.iter_photo_fields(definition) == [("site_photos", "Site Photos", 3)]


def test_iter_photo_fields_max_count_clamped():
    definition = {"sections": [{"type": "header", "fields": [
        {"key": "a", "input": "photo", "label": "A", "max_count": 99},
        {"key": "b", "input": "photo", "label": "B"},  # missing → default 4
    ]}]}
    assert photo_screen.iter_photo_fields(definition) == [("a", "A", 4), ("b", "B", 4)]


def test_iter_photo_fields_none_when_absent():
    assert photo_screen.iter_photo_fields({"sections": []}) == []
    assert photo_screen.iter_photo_fields({}) == []


# ── build_caption ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "name,taken,gps,expected",
    [
        ("front.jpg", "2026-06-12T09:30:00", "34.05,-118.24", "front.jpg · 2026-06-12 09:30:00 · 34.05,-118.24"),
        ("", "", "", ""),
        ("only-name.jpg", "", "", "only-name.jpg"),
        ("", "2026-06-12T09:30", "", "2026-06-12 09:30"),
    ],
)
def test_build_caption(name, taken, gps, expected):
    assert photo_screen.build_caption(name, taken, gps) == expected


# ── CO-2: LIVE clamd EICAR smoke (prove-the-control-bites, HOUSE_REFLEXES §2) ──
#
# Everything above PATCHES `_clamav_scan`, so no test ever proved the real
# pyclamd → clamd wiring detects anything. These two tests run the REAL daemon:
# skipped wherever clamd isn't installed (CI, a dev Mac without ClamAV), and
# exercised on the production Mac at the Phase-C hardening gate, when the
# operator installs ClamAV and flips `safety_reports.photo_screen.clamav_enabled`.
# Run explicitly: pytest tests/test_photo_screen.py -k live_clamd -v


def _live_clamd_available() -> bool:
    try:
        import pyclamd  # noqa: PLC0415 — optional operator-installed dep

        return bool(pyclamd.ClamdUnixSocket().ping())
    except Exception:
        return False


_needs_live_clamd = pytest.mark.skipif(
    not _live_clamd_available(), reason="live clamd not available (operator-run smoke)"
)


@_needs_live_clamd
def test_live_clamd_flags_eicar():
    """The real daemon must FIND the canonical EICAR probe — this is the bite test
    for the L3 leg: a misconfigured clamd (stale signatures, wrong socket) fails
    here instead of silently passing malicious uploads."""
    verdict, sig = photo_screen._clamav_scan(EICAR.encode("ascii"))
    assert verdict == "FOUND"
    assert sig and "eicar" in sig.lower()


@_needs_live_clamd
def test_live_clamd_end_to_end_clean_photo():
    """A real photo through the FULL ladder with the live daemon: proves the
    clamav_enabled=True path completes (socket, protocol, tristate handling)
    on a clean input — the wiring smoke to pair with the EICAR bite above."""
    res = photo_screen.screen_photo(_jpeg(), clamav_enabled=True)
    assert res.disposition == "clean"
    assert res.layer == "L3"


# ---- 0074: make_thumbnail (the WPR picker thumb) ---------------------------


def _big_jpeg(px: int = 2000) -> bytes:
    import io as _io

    from PIL import Image
    img = Image.new("RGB", (px, px // 2), (10, 120, 200))
    out = _io.BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue()


def test_make_thumbnail_produces_bounded_jpeg_with_320_long_edge():
    import io as _io

    from PIL import Image
    thumb = photo_screen.make_thumbnail(_big_jpeg())
    assert thumb is not None
    assert thumb[:3] == b"\xff\xd8\xff"
    assert len(thumb) <= 40_000  # the Worker-side THUMB_MAX_BYTES bound
    w, h = Image.open(_io.BytesIO(thumb)).size
    assert max(w, h) <= photo_screen.THUMB_LONG_EDGE


def test_make_thumbnail_never_raises_on_garbage():
    assert photo_screen.make_thumbnail(b"not an image at all") is None
    assert photo_screen.make_thumbnail(b"") is None
