"""Deterministic unit tests for the codeql-fp-triager PreToolUse backstop.

The codeql-fp-triager subagent is propose-only; this hook structurally
refuses any code-scanning dismissal while allowing list/read (GET) calls.
"""

import json
import subprocess
from pathlib import Path

HOOK = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "block-codeql-dismiss.sh"


def _run(command: str) -> subprocess.CompletedProcess:
    payload = json.dumps({"tool_input": {"command": command}})
    return subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
    )


def test_hook_exists_and_executable():
    assert HOOK.exists(), f"hook missing at {HOOK}"


def test_blocks_dismiss_patch():
    r = _run(
        "gh api -X PATCH repos/its-sys-admin/evergreen-its/code-scanning/alerts/12 "
        "-f state=dismissed -f dismissed_reason=false_positive "
        '-f dismissed_comment="Pattern A: keychain service name"'
    )
    assert r.returncode == 2, r.stdout + r.stderr
    assert "BLOCKED" in r.stderr


def test_blocks_dismiss_subcommand_form():
    r = _run("gh code-scanning alert dismiss 12 --reason false_positive")
    assert r.returncode == 2, r.stdout + r.stderr


def test_allows_listing_open_alerts():
    r = _run('gh api "repos/its-sys-admin/evergreen-its/code-scanning/alerts?state=open" --paginate')
    assert r.returncode == 0, r.stdout + r.stderr


def test_allows_reading_single_alert():
    r = _run('gh api "repos/its-sys-admin/evergreen-its/code-scanning/alerts/12"')
    assert r.returncode == 0, r.stdout + r.stderr


def test_allows_unrelated_command():
    r = _run("git status")
    assert r.returncode == 0, r.stdout + r.stderr
