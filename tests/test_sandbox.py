"""Unit tests for the bwrap argv builder."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agonsr" / "scripts"))

import sandbox  # noqa: E402


def pairs(argv: list[str], flag: str) -> list[tuple[str, str]]:
    """Every (src, dest) pair passed to a two-argument bwrap flag."""
    return [(argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a == flag]


@pytest.fixture
def dirs(tmp_path):
    work = tmp_path / "project" / "0001"
    home = tmp_path / "scratch"
    for d in (work, home):
        d.mkdir(parents=True)
    return work, home


def test_starts_with_bwrap_binary(dirs):
    """Ubuntu's AppArmor exemption is granted to this binary by name."""
    work, home = dirs
    argv = sandbox.build_argv(work_dir=work, home_dir=home)
    assert argv[0] == "bwrap"


def test_ends_with_separator(dirs):
    work, home = dirs
    argv = sandbox.build_argv(work_dir=work, home_dir=home)
    assert argv[-1] == "--"


def test_unshares_every_namespace(dirs):
    work, home = dirs
    argv = sandbox.build_argv(work_dir=work, home_dir=home)
    assert "--unshare-all" in argv


def test_network_shared_by_default_for_the_proxy(dirs):
    work, home = dirs
    argv = sandbox.build_argv(work_dir=work, home_dir=home)
    assert "--share-net" in argv
    assert argv.index("--unshare-all") < argv.index("--share-net")


def test_network_can_be_withheld(dirs):
    work, home = dirs
    argv = sandbox.build_argv(work_dir=work, home_dir=home, share_net=False)
    assert "--share-net" not in argv


def test_work_dir_is_writable_at_its_own_path(dirs):
    """Identity mapping keeps the absolute paths in subagent prompts valid."""
    work, home = dirs
    argv = sandbox.build_argv(work_dir=work, home_dir=home)
    assert (str(work), str(work)) in pairs(argv, "--bind")


def test_home_dir_is_writable(dirs):
    work, home = dirs
    argv = sandbox.build_argv(work_dir=work, home_dir=home)
    assert (str(home), str(home)) in pairs(argv, "--bind")


def test_ro_paths_are_read_only_at_their_own_paths(dirs, tmp_path):
    work, home = dirs
    problem = tmp_path / "project" / "problem.md"
    problem.write_text("x")
    data = tmp_path / "project" / "data"
    data.mkdir()

    argv = sandbox.build_argv(work_dir=work, home_dir=home,
                              ro_paths=[problem, data])
    ro = pairs(argv, "--ro-bind-try")
    assert (str(problem), str(problem)) in ro
    assert (str(data), str(data)) in ro
    assert (str(problem), str(problem)) not in pairs(argv, "--bind")


def test_no_ro_paths_mounts_nothing_extra(dirs):
    """An origin candidate has no ancestors; it must not gain stray mounts."""
    work, home = dirs
    bare = sandbox.build_argv(work_dir=work, home_dir=home)
    system_ro = len(sandbox.SYSTEM_RO)
    assert len(pairs(bare, "--ro-bind-try")) == system_ro


def test_ancestors_are_mounted_individually(dirs, tmp_path):
    work, home = dirs
    ancestors = []
    for cid in ("0001", "0002"):
        a = tmp_path / "project" / cid / "ansatz.md"
        a.parent.mkdir(parents=True, exist_ok=True)
        a.write_text("report")
        ancestors.append(a)

    argv = sandbox.build_argv(work_dir=work, home_dir=home, ro_paths=ancestors)
    ro = pairs(argv, "--ro-bind-try")
    for a in ancestors:
        assert (str(a), str(a)) in ro


def test_siblings_and_search_state_are_never_mounted(dirs, tmp_path):
    """The kernel half of the double insurance: non-ancestors have no path."""
    work, home = dirs
    ancestor = tmp_path / "project" / "0001" / "ansatz.md"
    ancestor.parent.mkdir(parents=True, exist_ok=True)
    ancestor.write_text("report")

    argv = sandbox.build_argv(work_dir=work, home_dir=home, ro_paths=[ancestor])
    joined = " ".join(argv)
    assert "0002" not in joined
    assert "state.json" not in joined
    assert f"{tmp_path}/project " not in joined + " "


def test_ro_path_duplicating_work_dir_is_dropped(dirs):
    """A read-only bind over the work dir would silently make it unwritable."""
    work, home = dirs
    argv = sandbox.build_argv(work_dir=work, home_dir=home, ro_paths=[work, home])
    assert (str(work), str(work)) not in pairs(argv, "--ro-bind-try")
    assert (str(home), str(home)) not in pairs(argv, "--ro-bind-try")


def test_repeated_ro_paths_are_deduplicated(dirs, tmp_path):
    work, home = dirs
    p = tmp_path / "project" / "problem.md"
    p.write_text("x")
    argv = sandbox.build_argv(work_dir=work, home_dir=home, ro_paths=[p, p, p])
    assert [d for _, d in pairs(argv, "--ro-bind-try")].count(str(p)) == 1


def test_tmp_is_a_sized_tmpfs(dirs):
    work, home = dirs
    argv = sandbox.build_argv(work_dir=work, home_dir=home, tmp_bytes=1234)
    i = argv.index("--tmpfs")
    assert argv[i + 1] == "/tmp"
    assert argv[i - 2:i] == ["--size", "1234"]


def test_environment_is_cleared_before_anything_is_set(dirs):
    """A leaked host variable could carry a token or a path back in."""
    work, home = dirs
    argv = sandbox.build_argv(work_dir=work, home_dir=home,
                              env={"HOME": str(home)})
    assert "--clearenv" in argv
    assert argv.index("--clearenv") < argv.index("--setenv")


def test_env_values_are_passed_through(dirs):
    work, home = dirs
    argv = sandbox.build_argv(work_dir=work, home_dir=home, env={
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:8730",
        "ANTHROPIC_API_KEY": "run-token",
    })
    got = dict(pairs(argv, "--setenv"))
    assert got["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8730"
    assert got["ANTHROPIC_API_KEY"] == "run-token"


def test_starts_in_the_work_dir(dirs):
    work, home = dirs
    argv = sandbox.build_argv(work_dir=work, home_dir=home)
    assert (argv[argv.index("--chdir") + 1]) == str(work)


def test_paths_with_spaces_stay_single_arguments(dirs, tmp_path):
    """argv is a list, never a shell string, so a space cannot split a path."""
    work = tmp_path / "my project" / "cand 0001"
    home = tmp_path / "scratch dir"
    for d in (work, home):
        d.mkdir(parents=True)

    argv = sandbox.build_argv(work_dir=work, home_dir=home)
    assert (str(work), str(work)) in pairs(argv, "--bind")
    assert " " in str(work)


def test_shell_metacharacters_in_paths_are_not_interpreted(tmp_path):
    work = tmp_path / "p; rm -rf ~" / "c$(id)"
    home = tmp_path / "home"
    for d in (work, home):
        d.mkdir(parents=True)

    argv = sandbox.build_argv(work_dir=work, home_dir=home)
    assert (str(work), str(work)) in pairs(argv, "--bind")
    assert argv.count(str(work)) == 3  # bind src, bind dest, chdir


def test_require_raises_when_bwrap_is_absent(monkeypatch):
    """Fail closed. An unconfined fallback would silently void the guarantee."""
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: None)
    with pytest.raises(sandbox.SandboxUnavailable):
        sandbox.require()


def test_require_passes_when_bwrap_is_present(monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: "/usr/bin/bwrap")
    sandbox.require()
