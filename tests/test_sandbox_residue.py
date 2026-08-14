"""The sandbox must leave the host exactly as it found it.

A single run leaking one mount, one process or one temp file is invisible.
The deployment machine is not rebooted for weeks and runs continuously, so
anything that leaks once accumulates. Every check here therefore runs a batch
of invocations and compares a before/after snapshot, not a single pass.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agonsr" / "scripts"))

import sandbox  # noqa: E402

pytestmark = pytest.mark.skipif(not sandbox.available(),
                                reason="bubblewrap not installed")

BATCH = 5


def mount_points() -> set[str]:
    text = Path("/proc/self/mountinfo").read_text()
    return {line.split()[4] for line in text.splitlines() if len(line.split()) > 4}


def own_processes() -> set[str]:
    """Our own bwrap/CLI processes, by pid. Other users' are none of our business."""
    out = subprocess.run(["ps", "-u", str(os.getuid()), "-o", "pid=,comm="],
                         capture_output=True, text=True).stdout
    return {line.strip() for line in out.splitlines()
            if any(name in line for name in ("bwrap", "claude", "codex", "screen"))}


def listing(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {str(p.relative_to(path)) for p in path.rglob("*")}


def run_batch(work: Path, home: Path, script: str, times: int = BATCH) -> None:
    argv = sandbox.build_argv(work_dir=work, home_dir=home,
                              env={"PATH": "/usr/bin:/bin", "HOME": str(home)})
    for _ in range(times):
        subprocess.run(argv + ["/bin/sh", "-c", script],
                       capture_output=True, text=True, timeout=60)


@pytest.fixture
def workspace(tmp_path):
    work = tmp_path / "cand"
    home = tmp_path / "home"
    for d in (work, home):
        d.mkdir()
    return work, home


def test_no_mount_points_are_left_behind(workspace):
    """A namespace that outlives its process would pin its mounts forever."""
    work, home = workspace
    before = mount_points()
    run_batch(work, home, "echo hi > out.txt; mkdir -p /tmp/x; echo y > /tmp/x/y")
    assert mount_points() == before


def test_no_processes_are_left_behind(workspace):
    work, home = workspace
    before = own_processes()
    run_batch(work, home, "echo hi")
    assert own_processes() - before == set()


def test_backgrounded_children_do_not_survive(workspace):
    """--die-with-parent is what stops a stray sleep from outliving the run."""
    work, home = workspace
    before = own_processes()
    run_batch(work, home, "sleep 300 & echo started", times=2)
    assert own_processes() - before == set()


def test_host_tmp_is_untouched(workspace):
    """dispatch.py used to drop codex output under the host's /tmp/$USER."""
    work, home = workspace
    host_tmp = Path("/tmp")
    before = {p.name for p in host_tmp.iterdir()}
    run_batch(work, home,
              "echo junk > /tmp/agonsr-residue-probe; mkdir -p /tmp/$USER; "
              "echo junk > /tmp/$USER/sr-proposer-deadbeef.txt")
    assert {p.name for p in host_tmp.iterdir()} - before == set()


def test_real_claude_config_dir_is_untouched(workspace):
    """Session logs must land in the scratch home, never in the user's own."""
    work, home = workspace
    real = Path.home() / ".claude"
    before = listing(real)
    run_batch(work, home,
              f"mkdir -p {real}/projects && echo junk > {real}/projects/x.jsonl")
    assert listing(real) == before


def test_real_codex_config_dir_is_untouched(workspace):
    work, home = workspace
    real = Path.home() / ".codex"
    before = listing(real)
    run_batch(work, home, f"mkdir -p {real}/sessions && echo junk > {real}/sessions/x")
    assert listing(real) == before


def test_writes_stay_inside_the_candidate_directory(workspace, tmp_path):
    work, home = workspace
    outside = tmp_path / "outside"
    outside.mkdir()
    before = listing(outside)
    run_batch(work, home, f"echo escaped > {outside}/leak.txt 2>/dev/null; true")
    assert listing(outside) == before


def test_scratch_home_absorbs_cli_session_files(workspace):
    """They have to go somewhere writable, and that somewhere is disposable."""
    work, home = workspace
    run_batch(work, home, "mkdir -p $HOME/.claude/projects && echo s > $HOME/.claude/projects/s.jsonl",
              times=1)
    assert (home / ".claude" / "projects" / "s.jsonl").exists()


def test_repeated_batches_do_not_accumulate(workspace):
    """The check that actually models the deployment box: many runs, no drift."""
    work, home = workspace
    run_batch(work, home, "echo warmup")
    mounts = mount_points()
    procs = own_processes()

    for _ in range(3):
        run_batch(work, home, "echo hi > out.txt; sleep 0.1 & true")
        assert mount_points() == mounts
        assert own_processes() - procs == set()
