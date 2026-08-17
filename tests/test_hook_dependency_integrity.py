"""Guard dependency-integrity tests — Op Stds §56 (fail-closed guards).

Why this file exists
--------------------
The pre-existing hook tests (``test_hook_block_*.py``) assert what each guard
BLOCKS when its trigger condition matches. None of them asserted what a guard
does when *its own dependencies* are unavailable.

That gap was load-bearing. Audit 2026-08-16 finding C-1: every hook in
``.claude/hooks/`` piped its stdin payload through ``jq`` with no availability
check and no ``set -euo pipefail``. With ``jq`` absent from PATH the parse
produced an empty variable, the ``grep`` match failed, and the script fell
through to ``exit 0`` — silently PERMITTING force-push, version-gated doctrine
writes, CodeQL alert dismissal, and stale Cloudflare deploys.

CI never caught it because ``ubuntu-latest`` ships ``jq``. A guard is only
meaningful if it blocks when it cannot evaluate; these tests pin that property.

Invariants
----------
- Every ``block-*.sh`` guard exits 2 (BLOCK) when ``jq`` is not on PATH.
- Every ``block-*.sh`` guard declares ``set -euo pipefail``.
- The allow-path is unchanged: a benign payload with ``jq`` present exits 0.

Failure modes
-------------
Pure subprocess assertions; no I/O beyond the hook scripts themselves. A
failure here means a guard has regressed to fail-open — treat as blocking.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_DIR = REPO_ROOT / ".claude" / "hooks"

# Guards that BLOCK (exit 2). `warn-*.sh` hooks are advisory and exempt.
BLOCKING_HOOKS: list[str] = sorted(p.name for p in HOOK_DIR.glob("block-*.sh"))

# Advisory hooks. Opposite contract to the above: they may never exit non-zero.
ADVISORY_HOOKS: list[str] = sorted(p.name for p in HOOK_DIR.glob("warn-*.sh"))

# Hooks that resolve the ~/its live-tree topology. `set -euo pipefail` turns a
# deliberately-tolerant `$(git -C "$HOME/its" ...)` into a fatal error on any
# host where that tree is absent — which is the normal state on every customer
# fork inheriting .claude/hooks/. See test_topology_hooks_tolerate_absent_live_tree.
TOPOLOGY_HOOKS: dict[str, str] = {
    "block-stale-cloudflare-deploy.sh": (
        '{"tool_name":"Bash","tool_input":{"command":"npx wrangler deploy"}}'
    ),
    "warn-live-daemon-tree.sh": "{}",
}

# A payload that carries every field any guard reads, so one payload exercises
# all of them. Under the dependency-absent case the guard must never read it.
PAYLOAD = (
    '{"tool_input":{"command":"git push --force origin main",'
    '"file_path":"/x/its-blueprint/doctrine/operational-standards.md"}}'
)

BLOCK = 2
ALLOW = 0


def _minimal_bin(tmp_path: Path) -> Path:
    """A PATH containing the shell utilities the hooks need — but NOT `jq`."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for tool in ("bash", "cat", "grep", "echo", "sed", "basename", "dirname", "tr"):
        resolved = shutil.which(tool)
        if resolved:
            (bindir / tool).symlink_to(resolved)
    assert shutil.which("jq", path=str(bindir)) is None, "fixture leaked jq into PATH"
    return bindir


def test_blocking_hooks_discovered() -> None:
    """Guard against an empty parametrization silently passing this whole file."""
    assert BLOCKING_HOOKS, f"no block-*.sh guards found under {HOOK_DIR}"


@pytest.mark.parametrize("hook_name", BLOCKING_HOOKS)
def test_guard_fails_closed_without_jq(hook_name: str, tmp_path: Path) -> None:
    """A guard that cannot evaluate its payload MUST block, never permit.

    Op Stds §56: a guard whose failure mode is "permit" is not a guard.
    """
    bindir = _minimal_bin(tmp_path)
    result = subprocess.run(
        ["bash", str(HOOK_DIR / hook_name)],
        input=PAYLOAD,
        capture_output=True,
        text=True,
        env={"PATH": str(bindir), "HOME": str(tmp_path)},
    )
    assert result.returncode == BLOCK, (
        f"{hook_name} returned {result.returncode} with `jq` absent from PATH — "
        f"expected {BLOCK} (BLOCK). A guard MUST fail closed when it cannot "
        f"evaluate its input (Op Stds §56).\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "BLOCKED" in result.stderr, (
        f"{hook_name} blocked but emitted no operator-legible reason on stderr"
    )


@pytest.mark.parametrize("hook_name", BLOCKING_HOOKS)
def test_guard_declares_strict_mode(hook_name: str) -> None:
    """Every guard sets `set -euo pipefail` so a mid-script failure cannot
    fall through to the trailing `exit 0`."""
    src = (HOOK_DIR / hook_name).read_text()
    assert "set -euo pipefail" in src, (
        f"{hook_name} does not declare `set -euo pipefail` — an unset variable "
        f"or failed command can reach the trailing `exit 0` and permit the "
        f"action the guard exists to block (Op Stds §56)."
    )


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
@pytest.mark.parametrize("hook_name", BLOCKING_HOOKS)
def test_guard_allow_path_unchanged(hook_name: str) -> None:
    """The fail-closed preamble must not break the allow path: a benign payload
    with `jq` present still exits 0."""
    result = subprocess.run(
        ["bash", str(HOOK_DIR / hook_name)],
        input='{"tool_input":{"command":"git status","file_path":"/tmp/scratch.md"}}',
        capture_output=True,
        text=True,
    )
    assert result.returncode == ALLOW, (
        f"{hook_name} returned {result.returncode} on a benign payload — "
        f"expected {ALLOW} (ALLOW). The §56 preamble has over-blocked.\n"
        f"stderr={result.stderr!r}"
    )


# --- strict-mode blast radius -------------------------------------------------
# `set -euo pipefail` is the right default for a guard, but it is NOT free: it
# converts every previously-tolerated command failure into an abort. The two
# tests below pin the cases where that conversion is WRONG, both found by
# running the guards on a host without the ~/its live tree (2026-08-17):
#
#   block-stale-cloudflare-deploy.sh  exited 128 instead of 0
#   warn-live-daemon-tree.sh          exited 1   instead of 0
#
# Neither is the fail-open case §56 addresses. An absent ~/its means the risk
# condition (deploying FROM a stale live tree) is structurally absent, not
# unevaluable — and every customer fork that inherits .claude/hooks/ is in
# exactly that state, so an abort there is a fork-propagating spurious error.


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
@pytest.mark.parametrize("hook_name", sorted(TOPOLOGY_HOOKS))
def test_topology_hooks_tolerate_absent_live_tree(
    hook_name: str, tmp_path: Path
) -> None:
    """A host with no ``$HOME/its`` must not make a topology hook abort.

    Regression pin: the §56 strict-mode preamble made
    ``branch=$(git -C "$HOME/its" ...)`` fatal. Both hooks now guard the
    substitution with ``|| true``; reverting that guard fails this test.
    """
    fake_home = tmp_path / "home-without-its"
    fake_home.mkdir()
    assert not (fake_home / "its").exists()

    result = subprocess.run(
        ["bash", str(HOOK_DIR / hook_name)],
        input=TOPOLOGY_HOOKS[hook_name],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(fake_home)},
    )
    assert result.returncode == ALLOW, (
        f"{hook_name} returned {result.returncode} on a host where "
        f"$HOME/its does not exist — expected {ALLOW}. The risk condition this "
        f"hook guards is structurally absent there; aborting emits a spurious "
        f"hook error on every customer fork that inherits .claude/hooks/.\n"
        f"stderr={result.stderr!r}"
    )


def test_advisory_hooks_discovered() -> None:
    """Guard against an empty parametrization silently passing the next test."""
    assert ADVISORY_HOOKS, f"no warn-*.sh advisory hooks found under {HOOK_DIR}"


@pytest.mark.parametrize("hook_name", ADVISORY_HOOKS)
def test_advisory_hook_never_blocks(hook_name: str, tmp_path: Path) -> None:
    """``warn-*.sh`` hooks are SessionStart advisories and must always exit 0.

    Their contract is the inverse of a guard's: SessionStart cannot block, so a
    non-zero exit is pure noise on every session start. This must hold with
    ``jq`` absent too — an advisory hook has nothing to fail closed about.
    """
    bindir = _minimal_bin(tmp_path)
    for label, env in (
        ("jq present", None),
        ("jq absent", {"PATH": str(bindir), "HOME": str(tmp_path)}),
    ):
        result = subprocess.run(
            ["bash", str(HOOK_DIR / hook_name)],
            input=PAYLOAD,
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == ALLOW, (
            f"{hook_name} returned {result.returncode} ({label}) — an advisory "
            f"hook must always exit {ALLOW}.\nstderr={result.stderr!r}"
        )
