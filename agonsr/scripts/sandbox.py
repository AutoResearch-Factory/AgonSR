#!/usr/bin/env python3
"""Confines one CLI invocation to a single MCTS candidate directory.

Builds a bubblewrap argv that starts from an empty root and mounts back only
what the subagent legitimately needs. Anything not mounted has no resolvable
path inside, so a stray read fails with ENOENT rather than being denied by a
policy someone has to keep up to date.

Paths are mapped onto themselves: a host path bound into the sandbox keeps its
original name. That way the absolute paths already embedded in the subagent
prompts stay valid and nothing has to be rewritten. What the agent sees is the
real tree with every branch it may not touch pruned away.

Bubblewrap must be invoked as the `bwrap` binary rather than by calling unshare
directly: Ubuntu 24.04 sets kernel.apparmor_restrict_unprivileged_userns=1 and
grants the exemption to that binary specifically.
"""
from __future__ import annotations

import shutil
from pathlib import Path

BWRAP = "bwrap"

# Enough of the system to run python and node, all read-only. Mounted with
# --ro-bind-try, so entries that a given distribution does not have cost
# nothing. /usr/local carries npm-installed CLIs; /etc/crypto-policies is
# what Fedora's openssl.cnf includes, and node fails to start without it.
SYSTEM_RO = ("/usr", "/usr/local",
             "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf",
             "/etc/localtime", "/etc/ssl", "/etc/pki", "/etc/ca-certificates",
             "/etc/crypto-policies", "/etc/alternatives")

# Distributions differ on which of these are real directories vs symlinks into
# /usr; --symlink is a no-op against the tmpfs root when the target is unused.
MERGED_USR_LINKS = (("usr/bin", "/bin"), ("usr/sbin", "/sbin"),
                    ("usr/lib", "/lib"), ("usr/lib64", "/lib64"))

DEFAULT_TMP_BYTES = 64 * 1024 * 1024


class SandboxUnavailable(RuntimeError):
    """Raised when the host cannot provide the isolation we promise."""


def available() -> bool:
    return shutil.which(BWRAP) is not None


def require() -> None:
    """Fail closed. Running unconfined is never an acceptable fallback here."""
    if not available():
        raise SandboxUnavailable(
            f"{BWRAP} not found; refusing to run a subagent unconfined. "
            "Install bubblewrap (Fedora: dnf install bubblewrap, "
            "Debian/Ubuntu: apt install bubblewrap)."
        )


def build_argv(
    *,
    work_dir: Path,
    home_dir: Path,
    ro_paths: list[Path] | tuple[Path, ...] = (),
    rw_paths: list[Path] | tuple[Path, ...] = (),
    ro_binds: list[tuple[Path, Path]] | tuple[tuple[Path, Path], ...] = (),
    env: dict[str, str] | None = None,
    tmp_bytes: int = DEFAULT_TMP_BYTES,
    share_net: bool = True,
) -> list[str]:
    """Return the bwrap prefix for a subagent command.

    work_dir  the candidate directory, the only writable persistent location
    home_dir  scratch HOME, bound writable so CLI session logs stay readable
              from the host (the transcript tail needs them) and so nothing is
              written into the user's real home
    ro_paths  host paths the subagent may read, mounted at their own names: the
              problem statement, the data directory, ancestor reports, the
              plugin tree, the CLI executable
    rw_paths  further writable host paths; used only when running by hand, to
              hand the CLIs their own config trees
    ro_binds  explicit (source, destination) pairs, for the few things that
              must appear somewhere other than where they live on the host
    env       variables to set inside; the environment is otherwise cleared
    """
    work_dir = Path(work_dir).resolve()
    home_dir = Path(home_dir).resolve()

    argv = [BWRAP, "--unshare-all"]
    if share_net:
        # The CLI still has to reach the credential proxy on loopback.
        argv.append("--share-net")
    argv += ["--die-with-parent", "--new-session"]

    for path in SYSTEM_RO:
        argv += ["--ro-bind-try", path, path]
    for target, link in MERGED_USR_LINKS:
        argv += ["--symlink", target, link]

    argv += ["--proc", "/proc", "--dev", "/dev"]
    argv += ["--size", str(int(tmp_bytes)), "--tmpfs", "/tmp"]

    argv += ["--bind", str(work_dir), str(work_dir)]
    argv += ["--bind", str(home_dir), str(home_dir)]

    seen = {work_dir, home_dir}
    for path in rw_paths:
        resolved = Path(path).resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        argv += ["--bind-try", str(resolved), str(resolved)]

    for path in ro_paths:
        resolved = Path(path).resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        argv += ["--ro-bind-try", str(resolved), str(resolved)]

    # Layered after the writable home so they can land inside it.
    for source, dest in ro_binds:
        argv += ["--ro-bind-try", str(Path(source)), str(Path(dest))]

    argv += ["--chdir", str(work_dir), "--clearenv"]
    for key, value in (env or {}).items():
        argv += ["--setenv", key, str(value)]

    argv.append("--")
    return argv
