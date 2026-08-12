"""Smoke tests: the mechanical doctrine-drift checker runs + parses the manifest.

Validates that `scripts/check_doctrine_drift.py` consumes
`docs/doctrine_manifest.yaml` and produces well-formed findings against the real
repo. Does NOT assert a finding count — the checker reports whatever drift exists
(its job); this only locks the contract (exit 0, parseable shape).
"""

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# scripts/ is not a Python package; use the same sys.path-insert idiom as
# tests/test_audit_picklist_drift.py so the module imports as the top-level
# `check_doctrine_drift` (a `from scripts import …` would make mypy see the file
# under two module names — "found twice").
SCRIPTS_DIR = REPO / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_doctrine_drift as cdd  # noqa: E402  — sys.path-driven import


def test_runs_and_emits_json():
    r = subprocess.run(
        [sys.executable, "-m", "scripts.check_doctrine_drift", "--json"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout)
    assert isinstance(data, list)
    for f in data:
        assert set(f) >= {"check", "severity", "location", "detail"}
        assert f["check"] in {"M1", "M2", "M3", "M4", "M5", "M6", "M7"}
        assert f["severity"] in {"drift", "coverage", "clean"}


def test_human_output_runs():
    r = subprocess.run(
        [sys.executable, "-m", "scripts.check_doctrine_drift"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert "PROPOSE-ONLY" in r.stdout
    assert "DRIFT" in r.stdout


def test_sheet_ids_are_clean():
    # The two canonical sheet IDs in shared/sheet_ids.py must match the manifest
    # (this is verified-clean state; a failure here is real M4 drift).
    r = subprocess.run(
        [sys.executable, "-m", "scripts.check_doctrine_drift", "--json"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    data = json.loads(r.stdout)
    assert [f for f in data if f["check"] == "M4"] == [], "unexpected sheet-ID drift (M4)"


def test_strict_passes_on_clean_main():
    """--strict must exit 0 on main: M1 (version) / M4 (sheet-id) / M7 (citation) are
    all clean. A failure here is REAL blocking drift to fix BEFORE merge (the CI gate)."""
    r = subprocess.run(
        [sys.executable, "-m", "scripts.check_doctrine_drift", "--strict"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"blocking doctrine drift on main:\n{r.stdout}\n{r.stderr}"


def test_default_invocation_stays_propose_only_exit_zero():
    """Without --strict the checker stays exit-0 propose-only (the agent + the two
    smoke tests above depend on it), even though M2 'drift' findings exist."""
    r = subprocess.run(
        [sys.executable, "-m", "scripts.check_doctrine_drift"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr


def test_m7_blocks_on_out_of_range_citation(tmp_path, monkeypatch):
    """M7 bites: an `Op Stds §<too-big>` citation resolves nowhere → blocking drift,
    and M7 is in the strict gate. Prove-it-bites at unit level (no real-repo edit)."""
    fake = tmp_path / "fake_doctrine.md"
    fake.write_text("Per Op Stds §999 the rule applies; see also Op Stds §3.\n")
    # _current_doctrine_files always returns repo files in production, so the
    # function's f.relative_to(REPO_ROOT) is safe there; point REPO_ROOT at tmp_path
    # for this fixture so the fake file resolves.
    monkeypatch.setattr(cdd, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(cdd, "_current_doctrine_files", lambda: [fake])
    manifest = {"doctrine_versions": {"operational_standards": {"max_section": 49}}}

    findings = cdd.check_citation_resolves(manifest)
    assert any(f.check == "M7" and "§999" in f.detail for f in findings), findings
    # §3 (<= 49) must NOT be flagged.
    assert not any("§3 " in f.detail for f in findings)
    assert "M7" in cdd.STRICT_BLOCKING_CHECKS


def test_workstream_count_matches_the_slug_list():
    """`workstreams.count` must equal `len(workstreams.slugs)`.

    Nothing in the checker reads `count` — M5 iterates `slugs` alone — so the field
    is pure documentation with no enforcement, and it drifted exactly as unenforced
    documentation does: it read 6 while the blueprint had 10 workstreams, omitting
    field_ops, progress_reports and operator_dashboard (all built and live) plus
    urs_marine_portal. Someone reading the manifest to answer "how many workstreams
    are there" got the wrong answer for months.

    This cannot detect the list falling behind the BLUEPRINT — that needs a
    cross-repo read the checker deliberately avoids so it works in a fresh CI clone.
    What it does guarantee is that the two halves of this one block can never
    disagree, so a slug added without bumping the count fails here.
    """
    manifest = cdd._load_manifest()
    slugs = manifest["workstreams"]["slugs"]
    count = manifest["workstreams"]["count"]
    assert count == len(slugs), (
        f"doctrine_manifest.yaml workstreams.count={count} but the slugs list has "
        f"{len(slugs)} entries: {slugs}"
    )
    assert len(set(slugs)) == len(slugs), f"duplicate workstream slugs: {slugs}"
