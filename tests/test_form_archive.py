"""Tests for the manual-fallback blank-form archive (PR-L).

Covers `form_pdf.render_blank_fillable` + `render_cover_sheet` (pure-bytes renderers)
and `scripts/generate_form_archive.py` (render-only). Asserts every form renders to a
valid, openable, field-bearing PDF and that static row tables emit EXACTLY `min_rows`
fillable rows. A CI-SKIPPED integration test exercises the live Box round-trip.
"""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pypdf
import pytest

from safety_reports.form_pdf import (
    load_definition,
    render_blank_fillable,
    render_cover_sheet,
)

_ROOT = Path(__file__).resolve().parents[1]
FORMS_DIR = _ROOT / "safety_portal" / "forms"
DEF_PATHS = sorted(p for p in FORMS_DIR.glob("*.json") if p.name != "meta-schema.json")


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _def(code: str) -> dict:
    """load_definition that asserts the form resolved (narrows dict|None → dict)."""
    d = load_definition(code)
    assert d is not None, f"definition {code!r} did not load"
    return d


def _fields(pdf_bytes: bytes) -> dict:
    return pypdf.PdfReader(io.BytesIO(pdf_bytes)).get_fields() or {}


def _row_table_sections(definition: dict) -> list[dict]:
    return [s for s in definition["sections"]
            if s["type"] in ("repeating_table", "signature_table")]


def _expects_acroform_fields(definition: dict) -> bool:
    """True when the blank renderer should emit at least one AcroForm widget.

    `photo` renders an honest bordered portal note (photos cannot ride a paper form)
    and `signature` a sign-by-hand baseline — NEITHER consumes an AcroForm field, so
    a definition whose only inputs are photo/signature legitimately renders a
    widget-free blank (e.g. photo-test-v1). Everything else (text / textarea / date /
    time / number / select, checklist items, freeform) emits widgets. Mirrors
    tests/test_render_smoke.py's widget-aware predicate."""
    widgetless = {"photo", "signature"}
    for s in definition.get("sections", []):
        typ = s.get("type")
        if typ in ("checklist", "freeform"):
            return True
        if typ == "header" and any(
            f.get("input", "text") not in widgetless for f in s.get("fields", [])
        ):
            return True
        if typ in ("repeating_table", "signature_table") and any(
            c.get("input", "text") not in widgetless for c in s.get("columns", [])
        ):
            return True
    return False


# ── every form renders to a valid, field-bearing fillable PDF ──────────────────
@pytest.mark.parametrize("path", DEF_PATHS, ids=lambda p: p.stem)
def test_blank_form_renders_valid_fillable_pdf(path: Path) -> None:
    definition = _load(path)
    out = render_blank_fillable(definition)
    assert out[:5] == b"%PDF-", "not a PDF"
    reader = pypdf.PdfReader(io.BytesIO(out))
    assert len(reader.pages) >= 1
    if _expects_acroform_fields(definition):
        # AcroForm fields exist (the whole point — a blank fillable form).
        assert reader.get_fields(), f"{path.stem} produced no AcroForm fields"
    else:
        # A photo/signature-only form must NOT sprout widgets — the photo note is
        # deliberately non-interactive (a widget here would be the old fake text box).
        assert not (reader.get_fields() or {}), (
            f"{path.stem} has only widgetless inputs but produced AcroForm fields"
        )


def test_form_definitions_present() -> None:
    """The glob must pick up the form definitions (meta-schema excluded). The COUNT is
    intentionally not asserted — the publish pipeline adds forms, so a hardcoded total
    is self-defeating (it red-CIs every new-form publish). Per-form validity is covered
    by the parametrized tests above."""
    assert DEF_PATHS, "no form definitions found"
    assert all(p.name != "meta-schema.json" for p in DEF_PATHS)


# ── static row tables emit EXACTLY min_rows fillable rows (no add/delete) ───────
@pytest.mark.parametrize("path", DEF_PATHS, ids=lambda p: p.stem)
def test_row_tables_emit_exactly_min_rows(path: Path) -> None:
    """For each repeating/signature table, the count of text-field WIDGETS for a given
    column equals min_rows. We pick a TEXT column (text/textarea/date/time/number) —
    signature columns render as a hand-sign LINE (not a field), so they're excluded.

    Counted PER SECTION, by rendering each table on its own. A whole-document count keyed
    on the field-name suffix CANNOT work: `_FieldNamer` names every field `f{n}_{key}` off
    a per-DOCUMENT counter (form_pdf.py `_FieldNamer.next`), so the suffix carries no
    section identity, and a column key reused across sections silently over-counts every
    table that shares it. Reuse is legal by contract — the Worker validates column keys
    with `localUnique(colKeys, "column")` (worker/publishValidation.ts), reserving
    cross-section uniqueness for top-level value keys alone — and the admin editor emits
    exactly that shape: every new table is seeded `col_1` (editorModel.blankSection) with
    the numbering restarting per section (FormEditor's `${keyHint}_${fields.length + 1}`).
    So any editor-authored form with two or more tables tripped the old document-wide
    count. Isolating the section also keeps the assertion from being satisfied by an
    offsetting distribution that merely sums to the right total.
    """
    definition = _load(path)
    for section in _row_table_sections(definition):
        solo = _fields(render_blank_fillable({**definition, "sections": [section]}))
        expect = section.get("min_rows", 1)
        for col in section["columns"]:
            if col["input"] == "signature":
                continue  # sign-by-hand line, not an AcroForm field
            key = col["key"]
            count = sum(1 for name in solo if name.split("_", 1)[-1] == key)
            assert count == expect, (
                f"{path.stem} {section['key']}.{key}: got {count} field rows, "
                f"expected min_rows={expect}"
            )


@pytest.mark.parametrize("path", DEF_PATHS, ids=lambda p: p.stem)
def test_full_document_row_field_totals(path: Path) -> None:
    """The REAL rendered document carries every table's rows.

    The per-section test above renders each table in ISOLATION, so it is structurally
    blind to a section dropped (or duplicated) when the whole form is assembled. This
    asserts the shipped artifact's totals. Counts are summed per column KEY because one
    key may legally appear in several sections (see the docstring above).
    """
    definition = _load(path)
    expected: dict[str, int] = {}
    for section in _row_table_sections(definition):
        for col in section["columns"]:
            if col["input"] == "signature":
                continue
            expected[col["key"]] = expected.get(col["key"], 0) + section.get("min_rows", 1)
    fields = _fields(render_blank_fillable(definition))
    for key, want in expected.items():
        count = sum(1 for name in fields if name.split("_", 1)[-1] == key)
        assert count == want, (
            f"{path.stem} column {key!r} across all row tables: got {count}, expected {want}"
        )


def test_jha_row_counts() -> None:
    """JHA: hazard_analysis 8 rows × 3 cols + worker_acknowledgement 4 rows × 2 fields
    (the signature column is a line, not a field) + 4 header fields = 36 text fields."""
    fields = _fields(render_blank_fillable(_def("jha-v1")))
    text_fields = {n: f for n, f in fields.items() if f.get("/FT") == "/Tx"}
    assert len(text_fields) == 36, f"JHA text-field count drifted: {len(text_fields)}"


def test_toolbox_signin_is_eight_rows() -> None:
    """Every toolbox talk sign_in has min_rows=8; one name field per row (signature is a
    hand-sign line) → 8 text fields."""
    for code in ("toolbox-talk-electrical-v1", "toolbox-talk-ppe-v1",
                 "toolbox-talk-back-sprains-v1", "toolbox-talk-hard-hat-v1",
                 "toolbox-talk-ergonomics-v1"):
        fields = _fields(render_blank_fillable(_def(code)))
        assert len([1 for f in fields.values() if f.get("/FT") == "/Tx"]) == 8, code


def test_visitor_log_is_fifteen_rows() -> None:
    """Visitor: header 2 + visitor_log 15×6 + notes 1 = 93 text fields."""
    fields = _fields(render_blank_fillable(_def("visitor-sign-in-v1")))
    assert len([1 for f in fields.values() if f.get("/FT") == "/Tx"]) == 93


# ── checklist: rated → checkboxes, select → dropdown, numeric/text → text field ─
def test_skid_steer_checklist_has_checkboxes_and_no_dropdown() -> None:
    fields = _fields(render_blank_fillable(_def("equipment-skid-steer-v1")))
    kinds = {f.get("/FT") for f in fields.values()}
    assert "/Btn" in kinds, "rated checklist items must render as checkboxes"
    # numeric items (NEXT OIL CHANGE — HOURS, HOURS ON MACHINE) → text fields
    assert "/Tx" in kinds


def test_hsse_select_renders_as_dropdown() -> None:
    """HSS&E risk_rating is the one `select` field across all forms → a /Ch dropdown
    carrying its options (High/Medium/Low)."""
    fields = _fields(render_blank_fillable(_def("hsse-work-observation-v1")))
    choices = [f for f in fields.values() if f.get("/FT") == "/Ch"]
    assert len(choices) == 1, "expected exactly one dropdown (risk_rating)"
    opts = choices[0].get("/Opt") or []
    assert "High" in opts and "Medium" in opts and "Low" in opts


# ── static_text / content_blocks render verbatim via the submission path ────────
def test_static_text_renders_verbatim_in_blank() -> None:
    """JHA's mandatory footer + equipment lock/tag-out legal text MUST appear in the
    blank form — they route through the SAME `_section_flowables` as the submission
    renderer, so they cannot diverge."""
    jha = render_blank_fillable(_def("jha-v1"))
    text = " ".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(jha)).pages)
    assert "REVIEW AND REVISE THE PLAN" in " ".join(text.split())

    skid = render_blank_fillable(_def("equipment-skid-steer-v1"))
    stext = " ".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(skid)).pages)
    assert "ALWAYS lock/tag-out unsafe equipment" in " ".join(stext.split())


def test_toolbox_content_blocks_body_renders_in_blank() -> None:
    out = render_blank_fillable(_def("toolbox-talk-electrical-v1"))
    text = " ".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(out)).pages)
    norm = " ".join(text.split())
    assert "5-MINUTE SAFETY TALK" in norm
    # A sentence from the verbatim talk body.
    assert "Arcs take place when there has been damage" in norm


def test_blank_render_is_deterministic_field_shape() -> None:
    """Two renders of the same form produce the same field NAMES (the counter is reset
    per render, so the shape is stable run-to-run)."""
    a = set(_fields(render_blank_fillable(_def("jha-v1"))))
    b = set(_fields(render_blank_fillable(_def("jha-v1"))))
    assert a == b


# ── cover sheet ────────────────────────────────────────────────────────────────
def test_cover_sheet_renders_instructions() -> None:
    out = render_cover_sheet()
    assert out[:5] == b"%PDF-"
    text = " ".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(out)).pages)
    norm = " ".join(text.split())
    assert "Manual Fallback" in norm
    assert "ITS_Active_Jobs" in norm  # look the contact up there
    assert "Safety-Reports contact" in norm


# ── the generate script (render-only; no network) ──────────────────────────────
def _load_script():
    spec = importlib.util.spec_from_file_location(
        "generate_form_archive", _ROOT / "scripts" / "generate_form_archive.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_script_definition_paths_match_registry_glob() -> None:
    mod = _load_script()
    paths = mod._form_definition_paths()
    # The script's own glob must see exactly the same set as the registry glob (DEF_PATHS)
    # — dynamic, so a new-form publish never trips it.
    assert {p.name for p in paths} == {p.name for p in DEF_PATHS}
    assert all(p.name != "meta-schema.json" for p in paths)


def test_script_filename_by_form_id() -> None:
    mod = _load_script()
    # Keyed by the UNIQUE definition id (the forms/<id>.json stem), NOT the human
    # form_name — so two definitions that share a form_name (a version bump, or a
    # same-named variant) never collide in the archive. See the regression below.
    assert mod._blank_pdf_filename("jha-v2") == "jha-v2 (fillable).pdf"
    assert (mod._blank_pdf_filename("equipment-skid-steer-test-v1")
            == "equipment-skid-steer-test-v1 (fillable).pdf")
    # `/` (a Box path separator) is sanitized defensively (a stem can't contain one).
    assert "/" not in mod._blank_pdf_filename("a/b")


def test_script_render_only_writes_all_pdfs(tmp_path, monkeypatch) -> None:
    """Default mode renders the cover + every form to the out dir and touches NO Box.
    If anything tried to import box_client/smartsheet at render-time this would surface
    as a real network attempt — render-only must stay pure.

    The count holds dynamically: each definition is named by its unique id (stem), so
    one PDF lands per def + the cover, regardless of duplicate form_names. (Before the
    id-keyed fix this assertion red-CI'd every version-bump / same-named-variant publish
    — the file count came up one short — which blocked the publish daemon's merge gate.)
    """
    mod = _load_script()
    out = tmp_path / "out"
    rc = mod.main(["--out-dir", str(out)])
    assert rc == 0
    pdfs = sorted(out.glob("*.pdf"))
    assert len(pdfs) == len(DEF_PATHS) + 1  # one per form (unique id) + the cover
    assert any(p.name.startswith("00") for p in pdfs)  # cover sorts first
    for p in pdfs:
        assert p.read_bytes()[:5] == b"%PDF-"


def test_same_form_name_defs_do_not_collide(tmp_path, monkeypatch) -> None:
    """Regression (req 9 + req 10): two definitions that SHARE a form_name — a version
    bump (jha-v1 + jha-v2, both "Job Hazard Analysis") or a same-named variant (two
    "Equipment Pre-Inspection — Skid Steer" rows) — must each get their OWN archive PDF,
    keyed by the unique definition id. Naming by form_name made the second silently
    OVERWRITE the first on write (a blank form vanished) AND failed the count assertion
    above, red-CI'ing every such publish through the daemon's full-repo-CI merge gate.
    """
    mod = _load_script()
    # Two real, valid definitions with IDENTICAL form_name but distinct ids/stems.
    src = json.loads((FORMS_DIR / "jha-v1.json").read_text())
    forms = tmp_path / "forms"
    forms.mkdir()
    for stem in ("widget-v1", "widget-v2"):
        d = dict(src)
        d["form_name"] = "Identical Widget Form"  # same name on purpose
        (forms / f"{stem}.json").write_text(json.dumps(d))
    monkeypatch.setattr(mod, "_FORMS_DIR", forms)

    out = tmp_path / "out"
    assert mod.main(["--out-dir", str(out)]) == 0
    names = sorted(p.name for p in out.glob("*.pdf"))
    # cover + one PER DEFINITION (no collision), keyed by id not form_name:
    assert names == sorted([
        mod._COVER_FILENAME,
        "widget-v1 (fillable).pdf",
        "widget-v2 (fillable).pdf",
    ])


def test_script_defaults_to_render_only(monkeypatch, tmp_path) -> None:
    """`_upload` must NEVER be called without the explicit --upload flag. We poison it
    so the default path failing to be render-only would raise."""
    mod = _load_script()
    monkeypatch.setattr(mod, "_upload", lambda *a, **k: pytest.fail("upload in default mode!"))
    assert mod.main(["--out-dir", str(tmp_path / "o")]) == 0


# ── live Box round-trip — CI-SKIPPED (write-access probe, Op Stds §30) ──────────
# Default `pytest -q` SKIPS this (pyproject addopts `-m 'not integration'`). Run with
# `pytest -m integration`; requires Box OAuth creds in Keychain. NOT executed in CI.
@pytest.mark.integration
def test_box_archive_round_trip() -> None:
    from shared import box_client
    try:
        client = box_client.get_client()
    except box_client.BoxError as e:
        pytest.skip(f"Box credentials unavailable: {e!r}")

    # Use a throwaway ITS-prefixed root so we never touch the live mirror tree.
    root = box_client.get_or_create_folder("0", "ITS _int_form_archive_sandbox")
    try:
        archive = box_client.get_or_create_folder(root, "00_Form_Archive")
        pdf = render_blank_fillable(_def("jha-v1"))
        name = "Job Hazard Analysis (fillable).pdf"
        first = box_client.upload_bytes_or_new_version(archive, name, pdf)
        # Re-run = version-on-conflict: SAME file id, no duplicate.
        second = box_client.upload_bytes_or_new_version(archive, name, pdf)
        assert second["id"] == first["id"]
        names = [it["name"] for it in box_client.list_folder(archive, limit=1000)
                 if it["type"] == "file"]
        assert names.count(name) == 1
    finally:
        client.folder(root).delete(recursive=True)
