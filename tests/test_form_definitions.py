"""Validate every Safety Portal form definition against the meta-schema.

`safety_portal/forms/*.json` are the single source of truth both renderers (the
TS display runtime + the Python PDF renderer) consume, so they MUST conform to
`forms/meta-schema.json`. This test is the enforcement.
"""
from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import jsonschema
import pytest

from safety_reports.publish_manifest import PublishApplyError, check_required_content

_ROOT = Path(__file__).resolve().parents[1]
FORMS_DIR = _ROOT / "safety_portal" / "forms"
REF_DIR = _ROOT / "safety_portal" / "reference_forms"
META = json.loads((FORMS_DIR / "meta-schema.json").read_text())
DEF_PATHS = sorted(p for p in FORMS_DIR.glob("*.json") if p.name != "meta-schema.json")
REQUIRED_CONTENT = json.loads((_ROOT / "safety_portal" / "required-content.json").read_text())


def _load(p: Path) -> dict:
    return json.loads(p.read_text())


def test_meta_schema_is_itself_valid_jsonschema() -> None:
    jsonschema.Draft202012Validator.check_schema(META)


def test_there_are_definitions() -> None:
    assert DEF_PATHS, "no form definitions found"


@pytest.mark.parametrize("path", DEF_PATHS, ids=lambda p: p.stem)
def test_definition_conforms_to_meta_schema(path: Path) -> None:
    jsonschema.validate(_load(path), META)


@pytest.mark.parametrize("path", DEF_PATHS, ids=lambda p: p.stem)
def test_definition_source_pdf_exists(path: Path) -> None:
    d = _load(path)
    assert (REF_DIR / d["source_pdf"]).exists(), f"{d['source_pdf']} not in reference_forms/"


def test_form_codes_are_unique() -> None:
    codes = [_load(p)["form_code"] for p in DEF_PATHS]
    assert len(codes) == len(set(codes)), "duplicate form_code"


@pytest.mark.parametrize("path", DEF_PATHS, ids=lambda p: p.stem)
def test_signature_tables_have_exactly_one_signature_column(path: Path) -> None:
    for s in _load(path)["sections"]:
        if s["type"] == "signature_table":
            sig = [c for c in s["columns"] if c["input"] == "signature"]
            assert len(sig) == 1, f"{path.stem}/{s['key']}: need exactly one signature column"


@pytest.mark.parametrize("path", DEF_PATHS, ids=lambda p: p.stem)
def test_checklist_groups_have_scale_and_items(path: Path) -> None:
    for s in _load(path)["sections"]:
        if s["type"] == "checklist":
            for g in s["groups"]:
                assert g["scale"], f"{path.stem}/{g['key']}: empty scale"
                assert g["items"], f"{path.stem}/{g['key']}: no items"


def test_seed_parent_types_present() -> None:
    """The five seed parent types must remain present. Asserted as a SUBSET, not an exact
    set — the publish pipeline adds new parent types, so an equality check would be
    self-defeating (red-CI every new-form-type publish)."""
    parents = {_load(p)["parent_form_code"] for p in DEF_PATHS}
    seed = {
        "jha", "equipment-preinspection", "toolbox-talk",
        "visitor-sign-in", "hsse-work-observation",
    }
    assert seed <= parents, f"seed parent type(s) missing: {sorted(seed - parents)}"


def test_jha_mandatory_footer_and_signature_present() -> None:
    d = _load(FORMS_DIR / "jha-v1.json")
    texts = [s["text"] for s in d["sections"] if s["type"] == "static_text"]
    assert any("REVIEW AND REVISE THE PLAN" in t for t in texts)
    assert any(s["type"] == "signature_table" for s in d["sections"])


def test_equipment_lockout_legal_text_present() -> None:
    for code in (
        "equipment-telehandler-v1",
        "equipment-skid-steer-v1",
        "equipment-excavator-360-v1",
        "equipment-gayk-piledriver-v1",
    ):
        d = _load(FORMS_DIR / f"{code}.json")
        texts = [s["text"] for s in d["sections"] if s["type"] == "static_text"]
        assert any("lock/tag-out" in t for t in texts), code


def test_equipment_telehandler_item_count() -> None:
    # The Telehandler tri-state checklist must keep all items (no silent drop).
    d = _load(FORMS_DIR / "equipment-telehandler-v1.json")
    checklist = next(s for s in d["sections"] if s["type"] == "checklist")
    total = sum(len(g["items"]) for g in checklist["groups"])
    assert total == 64, f"expected 64 telehandler items, got {total}"


def test_equipment_excavator_360_item_count() -> None:
    # The 16 check rows of the source week-grid sheet — no silent drop.
    d = _load(FORMS_DIR / "equipment-excavator-360-v1.json")
    checklist = next(s for s in d["sections"] if s["type"] == "checklist")
    total = sum(len(g["items"]) for g in checklist["groups"])
    assert total == 16, f"expected 16 excavator items, got {total}"


def test_equipment_gayk_piledriver_item_count() -> None:
    # 12 checklist items = the source's 13 daily bullets minus the final free-text
    # block, which is a freeform SECTION (operating_issues), not a checklist item.
    d = _load(FORMS_DIR / "equipment-gayk-piledriver-v1.json")
    checklist = next(s for s in d["sections"] if s["type"] == "checklist")
    total = sum(len(g["items"]) for g in checklist["groups"])
    assert total == 12, f"expected 12 piledriver items, got {total}"
    assert any(
        s["type"] == "freeform" and s["key"] == "operating_issues" for s in d["sections"]
    )


def test_blank_checklist_comment_boxes_match_the_spa_rule() -> None:
    """The blank fillable must give a comment box to exactly the items the SPA does.

    The SPA rule is `it.comment ?? group.comment_per_item ?? false` (FormRenderer.tsx
    GroupView). The blank renderer builds its Comments COLUMN when the group flag is
    set OR any single item opts in — so in a `comment_per_item: false` group its
    per-item default must be the group flag, not True, or every non-opted-in item
    gets a hand-fill box the on-screen form never shows.

    Exercised by equipment-gayk-piledriver-v1, the first mixed-shape group (5 of 12
    items opt in); asserted over every shipped definition so a future one cannot
    reintroduce the drift.
    """
    from safety_reports import form_pdf

    for path in DEF_PATHS:
        d = _load(path)
        for section in d["sections"]:
            if section.get("type") != "checklist":
                continue
            for group in section["groups"]:
                group_flag = bool(group.get("comment_per_item"))
                want_column = group_flag or any(i.get("comment") for i in group["items"])
                for item in group["items"]:
                    spa = item.get("comment", group.get("comment_per_item", False))
                    pdf = want_column and item.get(
                        "comment", bool(group.get("comment_per_item"))
                    )
                    assert bool(spa) == bool(pdf), (
                        f"{d['form_code']} group {group['key']} item {item['key']}: "
                        f"SPA shows comment={bool(spa)} but the blank PDF shows "
                        f"comment={bool(pdf)} — the two renderers must not drift"
                    )
    # Pin the implementation the rule above mirrors, so a future edit of form_pdf
    # cannot silently restore the True default while this test still passes.
    src = inspect.getsource(form_pdf._blank_checklist_section)
    assert 'it.get("comment", bool(g.get("comment_per_item")))' in src, (
        "form_pdf._blank_checklist_section no longer defaults a checklist item's "
        "comment box to the GROUP flag — it will drift from the SPA again"
    )


def test_equipment_scales_render_with_recognised_response_words() -> None:
    """Every equipment pre-inspection scale value must colour in the filed PDF.

    `form_pdf._response_hex` colours only vocabulary it recognises; an unlisted word
    falls through to neutral ink, so a "Defective" answer would render the same as an
    "Okay" one — a scannability regression on exactly the answer a reviewer must not
    miss. This pins the two sets together so a future variant cannot introduce an
    uncoloured scale word silently.
    """
    from safety_reports.form_pdf import _BAD_WORDS, _NA_WORDS, _OK_WORDS

    known = _OK_WORDS | _BAD_WORDS | _NA_WORDS
    for path in DEF_PATHS:
        d = _load(path)
        if d.get("parent_form_code") != "equipment-preinspection":
            continue
        for section in d["sections"]:
            if section.get("type") != "checklist":
                continue
            for group in section["groups"]:
                for word in group["scale"]:
                    assert word.strip().upper() in known, (
                        f"{d['form_code']} group {group['key']}: scale word {word!r} is "
                        "not in form_pdf's _OK_WORDS/_BAD_WORDS/_NA_WORDS, so it renders "
                        "as neutral ink instead of green/amber/grey"
                    )


def test_hsse_has_eleven_assessment_categories() -> None:
    d = _load(FORMS_DIR / "hsse-work-observation-v1.json")
    s1 = next(s for s in d["sections"] if s.get("key") == "section_1")
    assert len(s1["groups"][0]["items"]) == 11


def test_toolbox_variants_have_content_and_signin() -> None:
    # Lower-bound, not exact: the 5 seed toolbox-talk variants must remain, but the publish
    # pipeline adds variants (an add_version under the existing parent writes a 6th
    # toolbox-talk-*.json into the globbed forms/ dir) — an `== 5` here would red-CI that
    # publish, the self-defeating gate Part D set out to remove. Each variant (seed or new)
    # must still carry the content_blocks + signature renderer contract.
    tb = [p for p in DEF_PATHS if _load(p)["parent_form_code"] == "toolbox-talk"]
    assert len(tb) >= 5
    for p in tb:
        d = _load(p)
        assert any(s["type"] == "content_blocks" and s["blocks"] for s in d["sections"])
        assert any(s["type"] == "signature_table" for s in d["sections"])


def _floor_without_section_types(rc: dict) -> dict:
    """The floor minus `required_section_types` — applied to HISTORICAL (non-current) shipped
    versions only. Section-type floors are the one key that is deliberately NOT the
    all-shipped-versions intersection (Slice 1, R3-F3: daily-report's job_requirements /
    expected_materials mounts were floored when v5 was already current, and v1-v4
    legitimately predate them). Historical files are frozen (append-only design), are not
    editor-reachable (the builder opens only current_form_code), and re-enter service only
    via rollback — which is deliberately floor-exempt (returns to a historically-valid
    version). Publish-time enforcement (both C3 layers) always applies the FULL floor."""
    rc = json.loads(json.dumps(rc))  # deep copy
    for group in ("parents", "identities"):
        for spec in (rc.get(group) or {}).values():
            spec.pop("required_section_types", None)
    (rc.get("defaults_for_new_identities") or {}).pop("required_section_types", None)
    return rc


@pytest.mark.parametrize("path", DEF_PATHS, ids=lambda p: p.stem)
def test_live_definition_satisfies_required_content(path: Path) -> None:
    """Every shipped definition satisfies its required-content legal floor (Brief 1 PR-1) — the
    generalized form of the per-form footer/lockout/signature assertions above, driven by
    safety_portal/required-content.json. check_required_content raises on a violation; a clean
    return is the pass. This locks the floor against future shipped forms too. CURRENT versions
    get the full floor; historical versions are exempt from `required_section_types` only
    (see _floor_without_section_types)."""
    d = _load(path)
    identity = re.sub(r"-v\d+$", "", d["form_code"])
    floor = (
        REQUIRED_CONTENT
        if d["form_code"] in CURRENT_FORM_CODES
        else _floor_without_section_types(REQUIRED_CONTENT)
    )
    try:
        check_required_content(
            d, identity=identity, parent_form_code=d["parent_form_code"],
            required_content=floor,
        )
    except PublishApplyError as exc:
        raise AssertionError(f"{path.stem}: {exc}") from exc


# ── guidance + form_link sections (SOP daily form, slice D1) ────────────────────
_CATALOG = json.loads((_ROOT / "safety_portal" / "catalog.json").read_text())
_CATALOG_PARENTS = {p["parent_form_code"] for p in _CATALOG["parents"]}

# The editor-reachable set: each ACTIVE identity's current_form_code (the builder's Edit /
# Add-version open only these). Shipped files NOT in this set are historical/retired —
# exempt from section-type floors in the glob test above (Slice 1, R3-F3).
CURRENT_FORM_CODES = {
    f["current_form_code"]
    for p in _CATALOG["parents"]
    for f in p["forms"]
    if f["status"] == "active"
}


@pytest.mark.parametrize("path", DEF_PATHS, ids=lambda p: p.stem)
def test_form_link_parents_exist_in_catalog(path: Path) -> None:
    """Every form_link section's parent_form_code must resolve to a catalog form type —
    the repo-side (live-HEAD) twin of the worker enqueue gate's KNOWN_PARENT_FORM_CODES
    check (a JSON Schema can't cross-file check, so this test is the enforcement)."""
    for s in _load(path)["sections"]:
        if s["type"] == "form_link":
            assert s["parent_form_code"] in _CATALOG_PARENTS, (
                f"{path.stem}: form_link → {s['parent_form_code']!r} is not a catalog form type"
            )


def test_daily_report_v2_sop_structure() -> None:
    """daily-report-v2 (the SOP daily form) carries the spec's structure: the SOP part
    headings verbatim, the three deep links, and the duty-confirm sections."""
    d = _load(FORMS_DIR / "daily-report-v2.json")
    headings = [s["heading"] for s in d["sections"] if s["type"] == "guidance"]
    for expected in (
        "7:30 AM — Arrive On Site — You Set the Tone",
        "A. Morning Kickoff — 1. Sign Workers In",
        "2. PPE Verification",
        "3. Complete the Daily JHA (Job Hazard Analysis)",
        "4. Visitor Log",
        "6. Electrical Safety",
        "7. General OSHA Compliance",
        "C. Quality Control — Verifying the Work",
        "13. Material & Equipment Deliveries",
        "14. Safety Oversight",
        "END OF DAY — Before Leaving the Site",
        "F. General Expectations & Standards of Conduct",
    ):
        assert expected in headings, f"missing SOP guidance heading: {expected!r}"
    # The three deep links (spec rows 4, 5, 12).
    links = [s["parent_form_code"] for s in d["sections"] if s["type"] == "form_link"]
    assert links == ["jha", "visitor-sign-in", "incident-report"]
    # The named callouts are present with their styles.
    callouts = {
        (b["style"], b["text"].split(":")[0])
        for s in d["sections"] if s["type"] == "guidance"
        for b in s["blocks"] if b["type"] == "callout"
    }
    assert ("note", "NOTE") in callouts
    assert ("critical", "CRITICAL RULE") in callouts
    assert ("quality", "QUALITY RULE") in callouts
    assert ("note", "FINAL STATEMENT") in callouts


def test_daily_report_v2_dfr_field_coverage() -> None:
    """Nothing lost vs the v1 Daily Field Report (the spec's coverage checklist):
    job_name/report_date moved to the submission envelope (job / work_date header
    fields); every other DFR datum keeps a value key in v2."""
    d = _load(FORMS_DIR / "daily-report-v2.json")
    keys: set[str] = set()
    for s in d["sections"]:
        if s["type"] == "header":
            keys.update(f["key"] for f in s["fields"])
        elif s["type"] == "checklist":
            keys.add(s["key"])
            for g in s["groups"]:
                keys.update(it["key"] for it in g["items"])
        elif s["type"] in ("repeating_table", "signature_table", "freeform"):
            keys.add(s["key"])
    # Envelope-bound header fields (the fill page / Daily tab provide these).
    assert {"job", "work_date"} <= keys
    # DFR coverage (spec): weather, average_temp, prepared_by, crew_progress,
    # tomorrows_goals, equipment_on_site, deliveries_received, site_visitors, comments.
    assert {
        "weather", "average_temp", "prepared_by", "crew_progress", "tomorrows_goals",
        "equipment_on_site", "deliveries_received", "site_visitors", "comments",
    } <= keys
    # The SOP duty confirms + tables added by v2 (spec rows 1-14).
    assert {
        "arrived_walkthrough", "workers_signed_in", "manpower_total", "ppe_verified",
        "trenching_inspected", "electrical_safe", "osha_walk_done", "qc_spot_checks",
        "photos_taken", "photos_uploaded", "safety_observations", "incidents_none",
        "cm_checkin_am", "cm_checkin_pm", "eod_secure",
    } <= keys
    # crew_progress keeps the v1 column keys (the S5 rollup prefill targets them).
    crew = next(s for s in d["sections"] if s.get("key") == "crew_progress")
    assert [c["key"] for c in crew["columns"]] == [
        "crew_subcontractor", "manpower", "todays_progress",
    ]


def test_daily_report_v3_photo_upload_replaces_minimum() -> None:
    """daily-report-v3 (slice D3, operator-directed 2026-07-02): the 50-photo daily
    minimum is removed and the photos_taken / photos_uploaded confirms are replaced by
    a direct 'Site photos' header photo field — the manager attaches the day's work
    photos inside the daily document. The DFR legal floor is untouched; v2 stays
    in-tree unchanged (append-only) and keeps its own tests above."""
    d = _load(FORMS_DIR / "daily-report-v3.json")
    assert d["version"] == 3 and d["form_code"] == "daily-report-v3"
    # The dated operator-deviation note rides the definition (meta-schema `comment`).
    assert any("OPERATOR-DIRECTED" in line for line in d.get("comment", []))

    # D.12: heading drops the minimum clause; no guidance text asserts 50 photos.
    headings = [s["heading"] for s in d["sections"] if s["type"] == "guidance"]
    assert "D. Throughout the Day — 12. Photo Documentation" in headings
    all_guidance_text = " ".join(
        text
        for s in d["sections"] if s["type"] == "guidance"
        for b in s["blocks"]
        for text in ([b["text"]] if "text" in b else b.get("items", []))
    )
    assert "Minimum 50" not in all_guidance_text and "50 photos" not in all_guidance_text
    assert "50+ photos" not in all_guidance_text
    # The WHAT-to-photograph guidance is kept.
    assert "progress milestones" in all_guidance_text
    assert "before and after correction" in all_guidance_text

    keys: set[str] = set()
    for s in d["sections"]:
        if s["type"] == "header":
            keys.update(f["key"] for f in s["fields"])
        elif s["type"] == "checklist":
            keys.add(s["key"])
            for g in s["groups"]:
                keys.update(it["key"] for it in g["items"])
        elif s["type"] in ("repeating_table", "signature_table", "freeform"):
            keys.add(s["key"])
    # The minimum-framed confirms are gone; the photo upload takes their place.
    assert "photos_taken" not in keys and "photos_uploaded" not in keys
    photo_section = next(
        s for s in d["sections"]
        if s["type"] == "header" and any(f["input"] == "photo" for f in s["fields"])
    )
    assert photo_section["title"] == "Site photos"
    assert [f["key"] for f in photo_section["fields"]] == ["site_photos"]
    # …at the D.12 position: immediately after the Photo Documentation guidance.
    idx = next(
        i for i, s in enumerate(d["sections"])
        if s.get("heading") == "D. Throughout the Day — 12. Photo Documentation"
    )
    assert d["sections"][idx + 1] is photo_section
    # DFR legal floor (required-content.json parents['daily-report']) still satisfied.
    assert {
        "weather", "average_temp", "prepared_by", "crew_progress", "tomorrows_goals",
        "equipment_on_site", "deliveries_received", "site_visitors", "comments",
    } <= keys
    # crew_progress keeps the v1 column keys (the S5 rollup prefill targets them).
    crew = next(s for s in d["sections"] if s.get("key") == "crew_progress")
    assert [c["key"] for c in crew["columns"]] == [
        "crew_subcontractor", "manpower", "todays_progress",
    ]


def test_daily_report_v4_job_requirements_placeholder() -> None:
    """daily-report-v4 (slice D4): v3 + ONE `job_requirements` placeholder section near the
    end (immediately before the F. General Expectations guidance), keyed `job_requirements`.
    The section carries NO content of its own — the per-job overlay (D1
    job_daily_requirements) is fetched at render time and the answers file under
    values.job_requirements. v3 stays in-tree unchanged (append-only) and keeps its own
    tests above; all SOP text is unchanged from v3."""
    d = _load(FORMS_DIR / "daily-report-v4.json")
    assert d["version"] == 4 and d["form_code"] == "daily-report-v4"
    # The dated D4 note rides the definition (meta-schema `comment`).
    assert any("SLICE D4" in line for line in d.get("comment", []))

    mounts = [s for s in d["sections"] if s["type"] == "job_requirements"]
    assert len(mounts) == 1, "exactly one job_requirements mount"
    assert mounts[0]["key"] == "job_requirements"
    assert mounts[0]["title"] == "Job-specific requirements"
    # Placement: near the end — immediately before the final F guidance section.
    idx = d["sections"].index(mounts[0])
    assert idx == len(d["sections"]) - 2
    last = d["sections"][-1]
    assert last["type"] == "guidance"
    assert last["heading"].startswith("F. General Expectations")

    # Everything else is v3 verbatim: same sections in the same order, the one insertion aside.
    v3 = _load(FORMS_DIR / "daily-report-v3.json")
    v4_minus_mount = [s for s in d["sections"] if s["type"] != "job_requirements"]
    assert v4_minus_mount == v3["sections"], "v4 must be v3 + ONLY the placeholder section"

    # The DFR field-key legal floor is untouched (the mount is a section, not a data field) —
    # but the section-TYPE floor now NAMES the mount (Slice 1, R3-F3): a future edit can never
    # silently amputate it. v4 itself is a historical version (current is v5), exempt from the
    # section-type floor in the glob test above.
    spec = REQUIRED_CONTENT["parents"]["daily-report"]
    assert "job_requirements" not in spec.get("required_field_keys", [])
    assert "job_requirements" in spec.get("required_section_types", [])


def test_daily_report_v5_expected_materials_mount() -> None:
    """daily-report-v5 (Material receipts M2): v4 + ONE `expected_materials` placeholder
    section in the D.13 deliveries region — immediately after the '13. Material & Equipment
    Deliveries' guidance and immediately before the Deliveries Received table — keyed
    `expected_materials_receipt`. The section carries NO content of its own AND files NO
    values under its key (the key is reserved for namespace uniqueness only): the Daily tab
    renders the job's expected materials (D1 job_expected_materials, migration 0031, M1)
    there; confirm-receipt appends a deliveries_received row instead, and problems file as
    material-incident submissions. v4 stays in-tree unchanged (append-only) and keeps its
    own tests above; all SOP text is unchanged from v4."""
    d = _load(FORMS_DIR / "daily-report-v5.json")
    assert d["version"] == 5 and d["form_code"] == "daily-report-v5"
    # The dated M2 note rides the definition (meta-schema `comment`).
    assert any("SLICE M2" in line for line in d.get("comment", []))

    mounts = [s for s in d["sections"] if s["type"] == "expected_materials"]
    assert len(mounts) == 1, "exactly one expected_materials mount"
    assert mounts[0]["key"] == "expected_materials_receipt"
    assert mounts[0]["title"] == "Expected materials"
    # Placement: the D.13 region — right after the deliveries guidance, before the table.
    idx = d["sections"].index(mounts[0])
    before = d["sections"][idx - 1]
    assert before["type"] == "guidance"
    assert before["heading"] == "13. Material & Equipment Deliveries"
    after = d["sections"][idx + 1]
    assert after["type"] == "repeating_table" and after["key"] == "deliveries_received"

    # Everything else is v4 verbatim: same sections in the same order, the one insertion aside.
    v4 = _load(FORMS_DIR / "daily-report-v4.json")
    v5_minus_mount = [s for s in d["sections"] if s["type"] != "expected_materials"]
    assert v5_minus_mount == v4["sections"], "v5 must be v4 + ONLY the placeholder section"

    # The DFR field-key legal floor is untouched (the mount's key files no values) — but the
    # section-TYPE floor now NAMES the mount (Slice 1, R3-F3): a future edit can never
    # silently amputate it.
    spec = REQUIRED_CONTENT["parents"]["daily-report"]
    assert "expected_materials_receipt" not in spec.get("required_field_keys", [])
    assert "expected_materials" in spec.get("required_section_types", [])


def test_amputated_daily_report_rejected_by_the_mac_c3_layer() -> None:
    """Slice 1, R3-F3 — the amputation guard, Mac half (publish_manifest.check_required_content;
    the Worker half is safety_portal/test/publish.test.ts). Stripping either definition-managed
    mount (job_requirements / expected_materials) from the CURRENT daily-report must be rejected
    with the floor's verbatim reason — a form-builder edit can never silently drop the D4/M2
    mounts. The intact v5 passes (positive control; also covered by the glob test above)."""
    v5 = _load(FORMS_DIR / "daily-report-v5.json")

    check_required_content(  # intact → clean return
        v5, identity="daily-report", parent_form_code="daily-report",
        required_content=REQUIRED_CONTENT,
    )

    for mount in ("job_requirements", "expected_materials"):
        amputated = json.loads(json.dumps(v5))
        amputated["sections"] = [s for s in amputated["sections"] if s["type"] != mount]
        with pytest.raises(PublishApplyError, match=f"must contain a '{mount}' section"):
            check_required_content(
                amputated, identity="daily-report", parent_form_code="daily-report",
                required_content=REQUIRED_CONTENT,
            )


def test_material_incident_v1_structure_and_floor() -> None:
    """material-incident-v1 (Material receipts M2): the manager-side delivery-problem form,
    deep-linked from the daily form's Expected-materials section and normally pickable from
    Submit-a-Form. NEW parent `material-incident`, catalog category 'progress' (commercial,
    not safety — operator-vetoable, noted in the definition comment). Fields per the M2
    spec; `issue` uses the meta-schema's SUPPORTED `select` input (verified: enum member +
    SPA FieldView dropdown + blank-mode AcroForm choice). Required-content floor (strict
    entry, OPERATOR-CONFIRMED 2026-07-03): material_description + issue + details."""
    d = _load(FORMS_DIR / "material-incident-v1.json")
    assert d["form_code"] == "material-incident-v1"
    assert d["parent_form_code"] == "material-incident"
    assert d["version"] == 1
    # Net-new form — no reference PDF exists (the photo-test-v1 precedent).
    assert d["source_pdf"] == ""
    # Flipped 2026-07-03: the operator confirmed the floor + category (proceed-with-defaults).
    assert any("OPERATOR-CONFIRMED" in line for line in d.get("comment", []))
    assert any("progress" in line for line in d.get("comment", []))

    # Header fields: description/ref/quantities/issue — issue is a bounded select.
    header = next(s for s in d["sections"] if s["type"] == "header" and "title" not in s)
    fields = {f["key"]: f for f in header["fields"]}
    assert fields["material_description"]["input"] == "text"
    assert fields["material_description"].get("required") is True
    assert fields["delivery_ref"]["input"] == "text"
    assert fields["qty_expected"]["input"] == "number"
    assert fields["qty_received"]["input"] == "number"
    assert fields["issue"]["input"] == "select"
    assert fields["issue"]["options"] == ["Damaged", "Short", "Wrong item", "Other"]
    assert fields["issue"].get("required") is True

    # details / action_taken are full-width textareas; photos is a header-level photo field
    # (the ONLY placement publishValidation allows photos — rides the §34 pipeline, D3).
    details = next(s for s in d["sections"] if s.get("key") == "details")
    assert details["type"] == "freeform" and details.get("input", "textarea") == "textarea"
    action = next(s for s in d["sections"] if s.get("key") == "action_taken")
    assert action["type"] == "freeform" and action.get("input", "textarea") == "textarea"
    photos = next(
        s for s in d["sections"]
        if s["type"] == "header" and any(f["input"] == "photo" for f in s["fields"])
    )
    assert [f["key"] for f in photos["fields"]] == ["photos"]

    # The catalog carries the new parent as category 'progress', normally pickable
    # (NO launch:'daily-tab' — that key is the daily-report parent's alone).
    parent = next(p for p in _CATALOG["parents"] if p["parent_form_code"] == "material-incident")
    assert parent["category"] == "progress"
    assert "launch" not in parent
    # SHAPE, not the editable current pointer (a version cut moves it — the self-defeating
    # pinned-current class, HOUSE_REFLEXES §5): v1 stays registered, and the pointer always
    # matches identity-v{current_version}.
    form = parent["forms"][0]
    assert {"version": 1, "form_code": "material-incident-v1"} in form["versions"]
    assert form["current_form_code"] == f"material-incident-v{form['current_version']}"

    # The required-content floor exists and names exactly the three floor fields; the
    # glob-parametrized test above proves the shipped definition satisfies it.
    spec = REQUIRED_CONTENT["parents"]["material-incident"]
    assert spec["required_field_keys"] == ["material_description", "issue", "details"]
    assert spec["required_signature_inputs_min"] == 0


def test_daily_report_v6_photo_pool_and_incident_link() -> None:
    """daily-report-v6 (DR-photo-pool Slice 1) — ONE cut carrying BOTH 2026-07-03 operator
    directives. (a) More photos: ONE `additional_photos` placeholder section immediately after
    the D.12 'Site photos' header — the 4-photo inline field STAYS untouched (payload-budgeted:
    CS2 280KB × 4 base64 < the Worker's 1.8MB payload cap, so more inline photos structurally
    cannot ride the submission); each extra photo uploads individually to the D1
    daily_photo_pool (migration 0037) and the submission carries only [{pool_id, caption?}]
    references under values.additional_photos, which /api/submit validates + claims. The key is
    the FIXED wire key 'additional_photos' (publishValidation enforces it — the Worker claims
    exactly that top-level key). (b) Incident link: ONE form_link → material-incident in the
    D.13 deliveries region, immediately after the Deliveries Received table (the operator's
    "link the material incident report underneath the material and equipment deliveries"). v5
    stays in-tree unchanged (append-only) and keeps its own tests above; all SOP text is
    unchanged from v5."""
    d = _load(FORMS_DIR / "daily-report-v6.json")
    assert d["version"] == 6 and d["form_code"] == "daily-report-v6"
    # The dated slice note rides the definition (meta-schema `comment`).
    assert any("SLICE DR-PHOTO-POOL" in line for line in d.get("comment", []))

    # (a) The pool mount — exactly one, the fixed wire key, right below the inline field.
    mounts = [s for s in d["sections"] if s["type"] == "additional_photos"]
    assert len(mounts) == 1, "exactly one additional_photos mount"
    assert mounts[0]["key"] == "additional_photos"
    assert mounts[0]["title"] == "Additional site photos"
    idx = d["sections"].index(mounts[0])
    before = d["sections"][idx - 1]
    assert before["type"] == "header" and before.get("title") == "Site photos"
    # The inline 4-photo field itself is UNTOUCHED (directive: "leave that four photo field").
    assert before["fields"] == [{"key": "site_photos", "label": "Site photos", "input": "photo"}]

    # (b) The incident link — under the D.13 Deliveries Received table.
    links = [
        s for s in d["sections"]
        if s["type"] == "form_link" and s["parent_form_code"] == "material-incident"
    ]
    assert len(links) == 1, "exactly one material-incident link"
    assert links[0]["label"] == "Report a material incident"
    li = d["sections"].index(links[0])
    assert d["sections"][li - 1]["type"] == "repeating_table"
    assert d["sections"][li - 1]["key"] == "deliveries_received"
    assert d["sections"][li + 1]["type"] == "repeating_table"
    assert d["sections"][li + 1]["key"] == "equipment_on_site"

    # Everything else is v5 verbatim: same sections in the same order, the two insertions aside.
    v5 = _load(FORMS_DIR / "daily-report-v5.json")
    v6_minus_cut = [
        s for s in d["sections"]
        if not (
            s["type"] == "additional_photos"
            or (s["type"] == "form_link" and s.get("parent_form_code") == "material-incident")
        )
    ]
    assert v6_minus_cut == v5["sections"], "v6 must be v5 + ONLY the two directive sections"

    # v6 is the catalog CURRENT (identity-keyed floor applies in full — the glob test above
    # proves it passes), and v5 dropped to historical (exempt from section-type floors only).
    assert "daily-report-v6" not in CURRENT_FORM_CODES  # superseded by v7
    assert "daily-report-v5" not in CURRENT_FORM_CODES
    # The D4/M2 floor mounts survive the cut (required_section_types names them).
    types = {s["type"] for s in d["sections"]}
    assert "job_requirements" in types and "expected_materials" in types
    # The floor's field keys are untouched: additional_photos files refs only when photos are
    # added — it is deliberately NOT a required field key.
    spec = REQUIRED_CONTENT["parents"]["daily-report"]
    assert "additional_photos" not in spec.get("required_field_keys", [])


def test_daily_report_v7_is_v6_plus_only_an_identity_change() -> None:
    """daily-report-v7 — the expected-materials section becomes VALUE-BEARING.

    The whole point of this cut is that the SECTIONS DO NOT CHANGE. v7 exists to mark a semantic
    change the section JSON cannot express: from v7 on the daily report FILES the day's
    expected-materials list under `expected_materials_receipt`, and the PDF prints it as a table.
    Asserting byte-identity mechanically (rather than eyeballing the diff) is what keeps a stray
    content edit from riding along under cover of a version bump.
    """
    v6 = _load(FORMS_DIR / "daily-report-v6.json")
    v7 = _load(FORMS_DIR / "daily-report-v7.json")
    assert v7["version"] == 7 and v7["form_code"] == "daily-report-v7"

    # BYTE-IDENTICAL sections — the load-bearing assertion of this cut.
    assert json.dumps(v7["sections"], sort_keys=True) == json.dumps(v6["sections"], sort_keys=True)
    # …and ONLY the three identity/provenance keys differ anywhere in the document.
    assert {k for k in set(v6) | set(v7) if v6.get(k) != v7.get(k)} == {
        "form_code", "version", "comment",
    }

    # v7 is the catalog CURRENT; v6 drops to historical (append-only — it stays in-tree).
    assert "daily-report-v7" in CURRENT_FORM_CODES
    assert (FORMS_DIR / "daily-report-v6.json").exists()
    # The D4/M2 floor mounts survive the cut.
    types = {s["type"] for s in v7["sections"]}
    assert "job_requirements" in types and "expected_materials" in types
    # Exactly ONE expected_materials mount, and it keeps the key the host seeds + the PDF reads.
    mounts = [s for s in v7["sections"] if s["type"] == "expected_materials"]
    assert len(mounts) == 1 and mounts[0]["key"] == "expected_materials_receipt"
