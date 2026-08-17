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

import pytest

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
        assert f["check"] in cdd.KNOWN_CHECKS
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


# ---- M8: is the MANIFEST ITSELF current? (audit 2026-08-16 H-5) ---------------
#
# Every other check validates the repo AGAINST the manifest. Nothing validated the
# manifest, and both its head pins were stale for weeks with no surface saying so.
# M8 closes that, and can only run where ../its-blueprint exists — which is never
# in CI, and is exactly why the gap survived.

_DOCTRINE_MD = """---
type: doctrine
version: {version}
status: canonical
---

# Operational Standards
"""


def _fake_world(tmp_path, *, recorded: int, live: int, head: str = "abc1234"):
    """A tmp repo + sibling blueprint, so M8 can be driven without touching either
    real checkout."""
    repo = tmp_path / "its"
    blueprint = tmp_path / "its-blueprint"
    (blueprint / "doctrine").mkdir(parents=True)
    (blueprint / "doctrine" / "operational-standards.md").write_text(
        _DOCTRINE_MD.format(version=live), encoding="utf-8"
    )
    repo.mkdir()
    manifest = {
        "meta": {"blueprint_head": head},
        "doctrine_versions": {
            "operational_standards": {
                "current": recorded,
                "source": "../its-blueprint/doctrine/operational-standards.md",
                "source_field": "frontmatter.version",
            }
        },
    }
    return repo, blueprint, manifest


def test_m8_is_in_the_strict_gate():
    assert "M8" in cdd.STRICT_BLOCKING_CHECKS


def test_m8_reports_coverage_not_drift_when_the_blueprint_is_absent(tmp_path, monkeypatch):
    """CI mode. The runner checks out ~/its alone, so M8 cannot run — it must SAY
    so rather than silently no-op, because a quiet skip is indistinguishable from
    a pass. And it must never block: CI has no way to satisfy it."""
    monkeypatch.setattr(cdd, "BLUEPRINT_ROOT", tmp_path / "nope")

    findings = cdd.check_manifest_freshness({"doctrine_versions": {}})

    assert [f.severity for f in findings] == ["coverage"]
    assert "NOT checked in this run" in findings[0].detail
    assert not [
        f for f in findings
        if f.check in cdd.STRICT_BLOCKING_CHECKS and f.severity == "drift"
    ]


def test_m8_blocks_when_the_manifest_records_a_stale_doctrine_version(
    tmp_path, monkeypatch
):
    """The headline case: doctrine bumped, the manifest did not. Every downstream
    claim derives from this number, so it is drift and it blocks."""
    repo, blueprint, manifest = _fake_world(tmp_path, recorded=20, live=21)
    monkeypatch.setattr(cdd, "REPO_ROOT", repo)
    monkeypatch.setattr(cdd, "BLUEPRINT_ROOT", blueprint)

    findings = cdd.check_manifest_freshness(manifest)
    drift = [f for f in findings if f.severity == "drift"]

    assert len(drift) == 1
    assert drift[0].check == "M8"
    assert "current=20" in drift[0].detail and "version=21" in drift[0].detail


def test_m8_is_silent_when_the_manifest_is_current(tmp_path, monkeypatch):
    repo, blueprint, manifest = _fake_world(tmp_path, recorded=21, live=21)
    monkeypatch.setattr(cdd, "REPO_ROOT", repo)
    monkeypatch.setattr(cdd, "BLUEPRINT_ROOT", blueprint)

    assert not [f for f in cdd.check_manifest_freshness(manifest) if f.severity == "drift"]


def test_m8_flags_a_source_path_that_no_longer_exists(tmp_path, monkeypatch):
    """A manifest naming a doctrine file that moved is silently unverifiable — the
    version comparison would just be skipped. It has to be loud."""
    repo, blueprint, manifest = _fake_world(tmp_path, recorded=21, live=21)
    manifest["doctrine_versions"]["operational_standards"]["source"] = (
        "../its-blueprint/doctrine/renamed-away.md"
    )
    monkeypatch.setattr(cdd, "REPO_ROOT", repo)
    monkeypatch.setattr(cdd, "BLUEPRINT_ROOT", blueprint)

    drift = [f for f in cdd.check_manifest_freshness(manifest) if f.severity == "drift"]
    assert len(drift) == 1
    assert "does not exist in the live blueprint" in drift[0].detail


def test_m8_head_pin_staleness_is_coverage_never_blocking(tmp_path, monkeypatch):
    """The blueprint's HEAD advances on every session log and most such commits
    touch no doctrine. Reporting the stale pointer is useful; BLOCKING on it would
    red-line the operator's local --strict several times a week — the §57
    alarm-fatigue failure that produced this audit's other findings."""
    repo, blueprint, manifest = _fake_world(tmp_path, recorded=21, live=21, head="dead000")
    monkeypatch.setattr(cdd, "REPO_ROOT", repo)
    monkeypatch.setattr(cdd, "BLUEPRINT_ROOT", blueprint)
    monkeypatch.setattr(cdd, "_blueprint_head", lambda: "beef111")

    findings = cdd.check_manifest_freshness(manifest)
    head_notes = [f for f in findings if "blueprint_head" in f.detail]

    assert len(head_notes) == 1
    assert head_notes[0].severity == "coverage", (
        "the head pin must never gate — see the docstring"
    )
    assert not [f for f in findings if f.severity == "drift"]


def test_strict_gate_ignores_coverage_severity(monkeypatch, capsys):
    """Regression pin.

    The strict filter originally keyed on the check ID alone, so M8's
    deliberately-non-blocking head-pin note failed --strict — a comment reading
    'never blocking' sitting over code that blocked, which is the exact
    narrated-not-enforced shape this work exists to remove. Found by running it.
    """
    coverage_only = [
        cdd.Finding("M8", "coverage", "docs/doctrine_manifest.yaml", "informational"),
    ]
    monkeypatch.setattr(cdd, "run_all", lambda: coverage_only)

    assert cdd.main(["--strict"]) == 0
    assert "no blocking drift" in capsys.readouterr().out


def test_strict_gate_still_blocks_on_drift_severity(monkeypatch, capsys):
    """The other half — tightening the filter must not disarm it."""
    monkeypatch.setattr(
        cdd, "run_all",
        lambda: [cdd.Finding("M8", "drift", "docs/doctrine_manifest.yaml", "stale")],
    )

    assert cdd.main(["--strict"]) == 1
    assert "BLOCKING drift" in capsys.readouterr().out


@pytest.mark.skipif(
    not (Path(__file__).resolve().parents[2] / "its-blueprint").is_dir(),
    reason="local mode only — CI has no sibling blueprint checkout",
)
def test_committed_manifest_is_current_against_the_live_blueprint():
    """The manifest in THIS commit must match live doctrine.

    Skipped in CI by necessity, which is the whole H-5 finding — so it runs
    wherever it can, and a merge from a machine with the blueprint present will
    catch a stale pin before it lands.
    """
    findings = cdd.check_manifest_freshness(cdd._load_manifest())
    drift = [f for f in findings if f.severity == "drift"]
    assert not drift, [f.detail for f in drift]


def test_ci_mode_json_contract_holds_with_the_blueprint_absent(monkeypatch) -> None:
    """The case a machine WITH ../its-blueprint cannot reach on its own.

    CI checks out ~/its alone, so M8 always emits its "could not run here"
    coverage line — and the full-suite run on a developer box, where the manifest
    is current and M8 is silent, never sees it. That divergence shipped a red CI
    on this very PR: the id assertion above was a hard-coded {M1..M7} literal.
    Simulating CI mode here is the only way the local suite can catch it.
    """
    monkeypatch.setattr(cdd, "BLUEPRINT_ROOT", Path("/nonexistent/its-blueprint"))

    findings = cdd.run_all()

    m8 = [f for f in findings if f.check == "M8"]
    assert len(m8) == 1, "CI mode must emit exactly one M8 line, not zero and not many"
    assert m8[0].severity == "coverage"
    for f in findings:
        assert f.check in cdd.KNOWN_CHECKS, f"undeclared check id {f.check!r}"
        assert f.severity in {"drift", "coverage", "clean"}


def test_known_checks_covers_every_id_the_module_actually_emits() -> None:
    """Registry parity in the other direction — a new check must join KNOWN_CHECKS."""
    emitted = {f.check for f in cdd.run_all()}
    undeclared = sorted(emitted - cdd.KNOWN_CHECKS)
    assert not undeclared, f"emitted check id(s) missing from KNOWN_CHECKS: {undeclared}"
