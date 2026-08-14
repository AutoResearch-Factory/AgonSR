"""Isolation behaviour, asserted against a real bubblewrap namespace.

These tests are the ones that would actually notice if the guarantee broke.
The unit tests only check that we built the argv we meant to build.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agonsr" / "scripts"))

import sandbox  # noqa: E402

pytestmark = pytest.mark.skipif(not sandbox.available(),
                                reason="bubblewrap not installed")


@pytest.fixture
def project(tmp_path):
    """A project laid out the way mcts.py lays one out."""
    root = tmp_path / "userdata" / "alice@example.com" / "proj"
    (root / "data").mkdir(parents=True)
    (root / "search").mkdir()
    (root / "0001").mkdir()
    (root / "0002").mkdir()
    (root / "0003").mkdir()
    home = tmp_path / "scratch"
    home.mkdir()

    (root / "problem.md").write_text("fit the data")
    (root / "data" / "measurements.csv").write_text("x,y\n1,2\n")
    (root / "search" / "state.json").write_text('{"secret": "tree layout"}')
    (root / "0001" / "ansatz.md").write_text("ancestor report")
    (root / "0002" / "ansatz.md").write_text("sibling report")
    return root, home


def run_in(work, home, script, ro_paths=()):
    """Run a shell snippet inside the sandbox, return its combined output."""
    argv = sandbox.build_argv(work_dir=work, home_dir=home, ro_paths=list(ro_paths),
                              env={"PATH": "/usr/bin:/bin", "HOME": str(home)})
    proc = subprocess.run(argv + ["/bin/sh", "-c", script],
                          capture_output=True, text=True, timeout=60)
    return proc.stdout + proc.stderr


def test_work_dir_is_writable_and_persists_to_the_host(project):
    root, home = project
    work = root / "0003"
    run_in(work, home, "echo written > result.txt")
    assert (work / "result.txt").read_text().strip() == "written"


def test_host_home_directory_does_not_exist(project):
    root, home = project
    out = run_in(root / "0003", home, f"ls {Path.home()} 2>&1")
    assert "No such file or directory" in out


def test_claude_credentials_have_no_path(project):
    """Not denied. Absent. There is nothing to enumerate or keep up to date."""
    root, home = project
    cred = Path.home() / ".claude" / ".credentials.json"
    out = run_in(root / "0003", home, f"cat {cred} 2>&1")
    assert "No such file or directory" in out
    assert "Permission denied" not in out


def test_codex_credentials_have_no_path(project):
    root, home = project
    cred = Path.home() / ".codex" / "auth.json"
    out = run_in(root / "0003", home, f"cat {cred} 2>&1")
    assert "No such file or directory" in out


def test_ssh_keys_have_no_path(project):
    root, home = project
    out = run_in(root / "0003", home, f"ls {Path.home() / '.ssh'} 2>&1")
    assert "No such file or directory" in out


def test_ancestor_report_is_readable(project):
    root, home = project
    out = run_in(root / "0003", home, f"cat {root / '0001' / 'ansatz.md'}",
                 ro_paths=[root / "0001" / "ansatz.md"])
    assert "ancestor report" in out


def test_ancestor_report_is_not_writable(project):
    root, home = project
    ancestor = root / "0001" / "ansatz.md"
    run_in(root / "0003", home, f"echo tampered > {ancestor} 2>&1",
           ro_paths=[ancestor])
    assert ancestor.read_text() == "ancestor report"


def test_sibling_candidate_has_no_path(project):
    """The kernel half of the double insurance the prompt also asks for."""
    root, home = project
    out = run_in(root / "0003", home, f"cat {root / '0002' / 'ansatz.md'} 2>&1",
                 ro_paths=[root / "0001" / "ansatz.md"])
    assert "sibling report" not in out
    assert "No such file or directory" in out


def test_search_state_has_no_path(project):
    root, home = project
    out = run_in(root / "0003", home, f"cat {root / 'search' / 'state.json'} 2>&1",
                 ro_paths=[root / "problem.md"])
    assert "tree layout" not in out
    assert "No such file or directory" in out


def test_project_listing_shows_only_what_was_mounted(project):
    root, home = project
    out = run_in(root / "0003", home, f"ls -A {root} 2>&1",
                 ro_paths=[root / "problem.md", root / "data"])
    assert "problem.md" in out
    assert "data" in out
    assert "0003" in out
    assert "0002" not in out
    assert "search" not in out


def test_another_users_directory_has_no_path(project, tmp_path):
    root, home = project
    other = tmp_path / "userdata" / "bob@example.com" / "proj"
    other.mkdir(parents=True)
    (other / "secret.md").write_text("bob's work")

    out = run_in(root / "0003", home, f"cat {other / 'secret.md'} 2>&1")
    assert "bob's work" not in out
    assert "No such file or directory" in out


def test_problem_and_data_are_readable(project):
    root, home = project
    out = run_in(root / "0003", home,
                 f"cat {root / 'problem.md'}; cat {root / 'data' / 'measurements.csv'}",
                 ro_paths=[root / "problem.md", root / "data"])
    assert "fit the data" in out
    assert "x,y" in out


def test_tmp_is_private_to_the_sandbox(project):
    root, home = project
    marker = "agonsr-isolation-probe.txt"
    run_in(root / "0003", home, f"echo leaked > /tmp/{marker}")
    assert not Path("/tmp", marker).exists()


def test_tmp_is_size_capped(project):
    root, home = project
    out = run_in(root / "0003", home,
                 "dd if=/dev/zero of=/tmp/fill bs=1M count=128 2>&1; echo rc=$?")
    assert "No space left on device" in out


def test_host_processes_are_invisible(project):
    root, home = project
    out = run_in(root / "0003", home, "ls /proc | grep -c '^[0-9]' || true")
    assert int(out.strip().splitlines()[-1]) < 10
