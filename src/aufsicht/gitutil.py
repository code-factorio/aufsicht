"""Thin subprocess wrapper around git.

All git access in the runner goes through here so that failure handling
is uniform: git being missing or failing is a tooling error (exit 3),
never a silent pass (v5.1 §4.6's fail-closed contract).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .errors import ToolingError


def git(
    *args: str,
    cwd: Path,
    check: bool = True,
    timeout: int | None = 120,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """Run git in *cwd*, capturing output. Raises ToolingError on failure."""
    exe = shutil.which("git")
    if exe is None:
        raise ToolingError(
            "git is not available on PATH",
            remedy="Install git; aufsicht requires a real git repository (v5.1 §2).",
        )
    try:
        proc = subprocess.run(
            [exe, *args],
            cwd=str(cwd),
            capture_output=True,
            text=text,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - defensive
        raise ToolingError(f"git {' '.join(args)} timed out") from exc
    if check and proc.returncode != 0:
        stderr = proc.stderr if isinstance(proc.stderr, str) else (proc.stderr or b"").decode("utf-8", "replace")
        stdout = proc.stdout if isinstance(proc.stdout, str) else (proc.stdout or b"").decode("utf-8", "replace")
        raise ToolingError(
            f"git {' '.join(args)} failed (exit {proc.returncode}): "
            f"{stderr.strip() or stdout.strip()}"
        )
    return proc


def is_git_repo(cwd: Path) -> bool:
    proc = git("rev-parse", "--is-inside-work-tree", cwd=cwd, check=False)
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def is_shallow(cwd: Path) -> bool:
    proc = git("rev-parse", "--is-shallow-repository", cwd=cwd, check=False)
    if proc.returncode != 0:
        raise ToolingError(
            "could not determine whether the repository is shallow",
            remedy="Check that this is a healthy git clone.",
        )
    return proc.stdout.strip() == "true"


def head_sha(cwd: Path) -> str:
    return git("rev-parse", "HEAD", cwd=cwd).stdout.strip()


def is_40_hex(value: str) -> bool:
    return len(value) == 40 and all(c in "0123456789abcdef" for c in value.lower())


def resolve_to_sha(value: str, cwd: Path) -> str | None:
    """Resolve a ref-or-sha string to a full commit SHA, or None.

    Accepts full SHAs directly and tries ref spellings a CI might hand
    us: the literal name, refs/<kind>/<name>, and <remote>/<name>.
    """
    if is_40_hex(value):
        verify = git("rev-parse", "--verify", "--quiet", f"{value}^{{commit}}", cwd=cwd, check=False)
        if verify.returncode == 0:
            return verify.stdout.strip()
        return None
    for candidate in (value, f"refs/heads/{value}", f"refs/remotes/{value}"):
        proc = git(
            "rev-parse", "--verify", "--quiet",
            f"{candidate}^{{commit}}", cwd=cwd, check=False,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    return None


def merge_base(sha_a: str, sha_b: str, cwd: Path) -> str | None:
    proc = git("merge-base", sha_a, sha_b, cwd=cwd, check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def changed_paths(base: str, cwd: Path) -> list[str]:
    """Repo-relative POSIX paths changed between *base* and the working
    tree (tracked changes only; untracked handled by the diff model)."""
    proc = git("diff", "--name-only", "-z", base, cwd=cwd)
    return [p for p in proc.stdout.split("\0") if p]


def untracked_files(cwd: Path) -> list[str]:
    proc = git("ls-files", "--others", "--exclude-standard", "-z", cwd=cwd)
    return [p for p in proc.stdout.split("\0") if p]


def branch_name(cwd: Path) -> str | None:
    proc = git("symbolic-ref", "--quiet", "--short", "HEAD", cwd=cwd, check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def default_branch(cwd: Path) -> str | None:
    """Best-effort name of the repository's default branch."""
    proc = git("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", cwd=cwd, check=False)
    if proc.returncode == 0:
        ref = proc.stdout.strip()
        if ref.startswith("refs/remotes/origin/"):
            return ref[len("refs/remotes/origin/"):]
    for name in ("main", "master"):
        if git("rev-parse", "--verify", "--quiet", f"refs/heads/{name}", cwd=cwd, check=False).returncode == 0:
            return name
    return None


def is_dirty(cwd: Path) -> bool:
    proc = git("status", "--porcelain", cwd=cwd)
    return bool(proc.stdout.strip())
