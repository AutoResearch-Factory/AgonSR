"""How dispatch.py wires each subagent into its sandbox."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agonsr" / "scripts"))

import dispatch  # noqa: E402


@pytest.fixture
def no_proxy(monkeypatch):
    monkeypatch.delenv(dispatch.PROXY_URL_ENV, raising=False)
    monkeypatch.delenv(dispatch.RUN_TOKEN_ENV, raising=False)


@pytest.fixture
def with_proxy(monkeypatch):
    monkeypatch.setenv(dispatch.PROXY_URL_ENV, "http://127.0.0.1:8730")
    monkeypatch.setenv(dispatch.RUN_TOKEN_ENV, "tok-abc")


@pytest.fixture
def cand(tmp_path):
    d = tmp_path / "proj" / "0003"
    d.mkdir(parents=True)
    return d


# ------------------------------------------------------------ ancestor paths

def test_no_ancestors_yields_no_paths():
    assert dispatch.ancestor_paths("none") == []
    assert dispatch.ancestor_paths("") == []


def test_ancestor_lines_are_parsed():
    text = ("- ancestor 1 (father): /p/search/0001/ansatz.md\n"
            "- ancestor 2 (grandfather): /p/search/0002/ansatz.md")
    assert dispatch.ancestor_paths(text) == [
        Path("/p/search/0001/ansatz.md"),
        Path("/p/search/0002/ansatz.md"),
    ]


def test_only_ansatz_files_are_extracted():
    """A stray path in the blob must not become a mount."""
    text = ("- ancestor 1 (father): /p/search/0001/ansatz.md\n"
            "note: see /etc/shadow for details")
    assert dispatch.ancestor_paths(text) == [Path("/p/search/0001/ansatz.md")]


def test_paths_with_spaces_survive_parsing():
    text = "- ancestor 1 (father): /p/my search/0001/ansatz.md"
    assert dispatch.ancestor_paths(text) == [Path("/p/my search/0001/ansatz.md")]


# ------------------------------------------------------------ codex provider

def test_codex_gets_no_provider_override_without_a_proxy(no_proxy):
    assert dispatch._codex_proxy_args() == []


def test_codex_is_pointed_at_the_proxy(with_proxy):
    args = dispatch._codex_proxy_args()
    joined = " ".join(args)
    assert 'model_provider="agonsr"' in joined
    assert "http://127.0.0.1:8730/v1" in joined
    assert 'wire_api="responses"' in joined
    assert f'env_key="{dispatch.RUN_TOKEN_ENV}"' in joined


def test_codex_provider_needs_both_url_and_token(monkeypatch):
    monkeypatch.setenv(dispatch.PROXY_URL_ENV, "http://127.0.0.1:8730")
    monkeypatch.delenv(dispatch.RUN_TOKEN_ENV, raising=False)
    assert dispatch._codex_proxy_args() == []


# ------------------------------------------------------------ sandbox session

def test_proxy_mode_keeps_credentials_out_of_the_namespace(with_proxy, cand):
    with dispatch._sandboxed("sh", cand, []) as sbx:
        joined = " ".join(sbx.argv)
    assert str(dispatch.CLAUDE_CREDENTIALS) not in joined
    assert str(dispatch.CODEX_CREDENTIALS) not in joined
    assert str(dispatch.CLAUDE_SETTINGS) not in joined


def test_proxy_mode_injects_the_run_token_as_the_api_key(with_proxy, cand):
    with dispatch._sandboxed("sh", cand, []) as sbx:
        env = dict(zip(sbx.argv, sbx.argv[1:]))
    assert "--setenv" in sbx.argv
    pairs = [(sbx.argv[i + 1], sbx.argv[i + 2])
             for i, a in enumerate(sbx.argv) if a == "--setenv"]
    got = dict(pairs)
    assert got["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8730"
    assert got["ANTHROPIC_API_KEY"] == "tok-abc"
    assert got[dispatch.RUN_TOKEN_ENV] == "tok-abc"


def test_standalone_mode_mounts_the_operators_own_config_trees(no_proxy, cand):
    """Running AgonSR by hand must keep working; the operator owns the keys."""
    with dispatch._sandboxed("sh", cand, []) as sbx:
        joined = " ".join(sbx.argv)
    for name in dispatch.STANDALONE_HOME_MOUNTS:
        assert str(Path.home() / name) in joined


def test_standalone_mode_still_hides_the_rest_of_the_home(no_proxy, cand):
    """Only the two config trees come back; everything else stays absent."""
    with dispatch._sandboxed("sh", cand, []) as sbx:
        joined = " ".join(sbx.argv)
    assert str(Path.home() / ".ssh") not in joined
    assert f"--bind-try {Path.home()} {Path.home()}" not in joined


def test_standalone_mode_sets_no_proxy_variables(no_proxy, cand):
    with dispatch._sandboxed("sh", cand, []) as sbx:
        joined = " ".join(sbx.argv)
    assert "ANTHROPIC_BASE_URL" not in joined


def test_proxy_mode_points_home_at_the_scratch_directory(with_proxy, cand):
    """Served runs get a disposable home so no session file outlives them."""
    with dispatch._sandboxed("sh", cand, []) as sbx:
        pairs = {sbx.argv[i + 1]: sbx.argv[i + 2]
                 for i, a in enumerate(sbx.argv) if a == "--setenv"}
        assert pairs["HOME"] == str(sbx.scratch)
        assert pairs["CLAUDE_CONFIG_DIR"] == str(sbx.scratch / ".claude")
        assert pairs["CODEX_HOME"] == str(sbx.scratch / ".codex")


def test_proxy_mode_scratch_is_not_under_tmp_when_runtime_dir_exists(with_proxy, cand):
    """codex refuses to set itself up when CODEX_HOME lives under /tmp."""
    if not dispatch._scratch_root():
        pytest.skip("no XDG_RUNTIME_DIR on this host")
    with dispatch._sandboxed("sh", cand, []) as sbx:
        assert not str(sbx.scratch).startswith("/tmp/")


def test_scratch_directory_is_removed_afterwards(no_proxy, cand):
    """Each invocation makes one; weeks of runs must not leave a pile."""
    with dispatch._sandboxed("sh", cand, []) as sbx:
        scratch = sbx.scratch
        assert scratch.exists()
    assert not scratch.exists()


def test_scratch_directory_is_removed_even_when_the_body_raises(no_proxy, cand):
    with pytest.raises(RuntimeError):
        with dispatch._sandboxed("sh", cand, []) as sbx:
            scratch = sbx.scratch
            raise RuntimeError("subagent blew up")
    assert not scratch.exists()


def test_each_invocation_gets_its_own_scratch(no_proxy, cand):
    with dispatch._sandboxed("sh", cand, []) as a:
        with dispatch._sandboxed("sh", cand, []) as b:
            assert a.scratch != b.scratch


def test_plugin_root_is_readable(no_proxy, cand):
    with dispatch._sandboxed("sh", cand, []) as sbx:
        joined = " ".join(sbx.argv)
    assert str(dispatch.PLUGIN_ROOT) in joined


def test_the_cli_executable_is_mounted(no_proxy, cand):
    """It lives under the user's home, which is otherwise not there at all."""
    with dispatch._sandboxed("sh", cand, []) as sbx:
        joined = " ".join(sbx.argv)
    assert str(sbx.exe) in joined


def test_wrap_replaces_the_bare_binary_with_its_bound_path(no_proxy, cand):
    with dispatch._sandboxed("sh", cand, []) as sbx:
        wrapped = sbx.wrap(["sh", "-c", "echo hi"])
    assert wrapped[0] == "bwrap"
    assert wrapped[-3:] == [str(sbx.exe), "-c", "echo hi"]


def test_requested_readable_paths_are_mounted(no_proxy, cand, tmp_path):
    problem = tmp_path / "proj" / "problem.md"
    problem.write_text("x")
    with dispatch._sandboxed("sh", cand, [problem]) as sbx:
        joined = " ".join(sbx.argv)
    assert str(problem) in joined


def test_missing_bwrap_refuses_to_run(no_proxy, cand, monkeypatch):
    monkeypatch.setattr(dispatch.sandbox.shutil, "which", lambda _: None)
    with pytest.raises(dispatch.sandbox.SandboxUnavailable):
        with dispatch._sandboxed("sh", cand, []):
            pass


def test_served_run_creates_the_cli_config_dirs(with_proxy, cand):
    """Both CLIs refuse to start when their config directory is missing, and a
    served run's home is a scratch directory that starts out empty. codex says
    'CODEX_HOME points to ... but that path does not exist' and exits 1."""
    with dispatch._sandboxed("sh", cand, []) as sbx:
        assert (sbx.home / ".claude").is_dir()
        assert (sbx.home / ".codex").is_dir()


def test_by_hand_run_leaves_the_real_config_dirs_alone(no_proxy, cand):
    with dispatch._sandboxed("sh", cand, []) as sbx:
        assert sbx.home == Path.home()


# ------------------------------------------------------- codex event rendering

def test_agent_message_is_rendered(capsys):
    dispatch._render_codex_event("proposer", json.dumps(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "y = x"}}))
    assert "says: y = x" in capsys.readouterr().out


def test_command_and_its_output_are_rendered(capsys):
    dispatch._render_codex_event("proposer", json.dumps(
        {"type": "item.completed",
         "item": {"type": "command_execution", "command": "ls -a",
                  "aggregated_output": "ansatz.md", "exit_code": 0}}))
    out = capsys.readouterr().out
    assert "ran: ls -a" in out
    assert "ansatz.md" in out


def test_session_id_comes_from_the_thread_event():
    got = dispatch._render_codex_event("proposer", json.dumps(
        {"type": "thread.started", "thread_id": "01a00389-d76c-7901"}))
    assert got == "01a00389-d76c-7901"


def test_unknown_events_render_nothing(capsys):
    """The whole point: an event we did not ask for stays out of the log rather
    than leaking into it. codex is free to add new ones."""
    for event in ({"type": "turn.started"},
                  {"type": "item.started", "item": {"type": "command_execution"}},
                  {"type": "turn.completed", "usage": {"input_tokens": 21115}},
                  {"type": "something.new", "prompt": "SECRET SYSTEM PROMPT"}):
        dispatch._render_codex_event("proposer", json.dumps(event))
    assert capsys.readouterr().out == ""


def test_malformed_lines_are_ignored(capsys):
    assert dispatch._render_codex_event("proposer", "not json at all") is None
    assert capsys.readouterr().out == ""


def test_errors_are_surfaced(capsys):
    dispatch._render_codex_event("proposer", json.dumps(
        {"type": "error", "message": "upstream refused"}))
    assert "error: upstream refused" in capsys.readouterr().out
