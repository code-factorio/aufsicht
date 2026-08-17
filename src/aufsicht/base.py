"""Base reference resolution (v5.1 §4.6) — fails closed.

Resolution order:

1. CI-provided base SHA (GITHUB_BASE_REF / equivalent; also covers
   merge queues, where HEAD is a synthetic merge commit)
2. QUALITY_BASE_REF from .quality/config.toml
3. remote HEAD — git symbolic-ref refs/remotes/<remote>/HEAD
4. TOOLING ERROR, exit 3

The report records the resolved identity, and all ratchet work operates
on that SHA rather than on a ref that could move mid-run. Never fall
back to analysing HEAD alone — that silently disables every ratchet
while the gate still reports green, which is the worst available
failure mode.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import gitutil
from .config import QualityConfig
from .errors import ToolingError

SOURCE_CI = "ci"
SOURCE_CONFIG = "config"
SOURCE_REMOTE_HEAD = "remote-head"

# CI base-ref variables, tried in order. Which variable carries the base
# differs per provider; the first one that is set wins. Values may be a
# branch name or a full SHA — both are resolved to a commit.
CI_ENV_CANDIDATES: tuple[str, ...] = (
    "GITHUB_BASE_SHA",          # GitHub Actions, pusher-provided immutable SHA
    "GITHUB_BASE_REF",          # GitHub Actions PRs (branch name)
    "CI_MERGE_REQUEST_DIFF_BASE_SHA",  # GitLab MRs (immutable SHA)
    "CI_MERGE_REQUEST_TARGET_BRANCH_NAME",  # GitLab MRs (branch name)
)


@dataclass(frozen=True)
class BaseRef:
    source: str   # "ci" | "config" | "remote-head"
    ref: str      # the ref as resolved, recorded in the report
    sha: str      # the commit every ratchet actually compared against


class BaseResolutionError(ToolingError):
    """Base could not be resolved. Exit 3 — never a silent pass."""


def _merge_base_or_die(head: str, base_sha: str, repo: Path, *, source: str, ref: str) -> BaseRef:
    mb = gitutil.merge_base(head, base_sha, repo)
    if mb is None:
        raise BaseResolutionError(
            f"no merge base between HEAD and {ref} ({base_sha})",
            remedy="Push the branch so the base ref is reachable, or set "
                   "[base] ref in .quality/config.toml (v5.1 §4.6).",
        )
    return BaseRef(source=source, ref=ref, sha=mb)


def resolve_base(repo: Path, config: QualityConfig) -> BaseRef:
    """Resolve the ratchet base for *repo*, per the §4.6 order."""
    if gitutil.is_shallow(repo):
        raise BaseResolutionError(
            "repository is a shallow clone; merge-base would fail or lie",
            remedy="Fetch full history: actions/checkout with fetch-depth: 0, "
                   "or `git fetch --unshallow` (v5.1 §4.6).",
        )

    head = gitutil.head_sha(repo)

    # 1. CI-provided base. Only CI variables here — QUALITY_BASE_REF is
    #    a config-file key per v5.1 §4.6, not an environment variable
    #    an incidental shell export could override.
    for var in tuple(config.base_ci_env):
        value = os.environ.get(var, "").strip()
        if not value:
            continue
        sha = gitutil.resolve_to_sha(value, repo)
        if sha is None:
            raise BaseResolutionError(
                f"{var}={value!r} does not resolve to a commit in this repository",
                remedy="Check the CI checkout fetched the base branch "
                       "(fetch-depth: 0), or unset the variable.",
            )
        return _merge_base_or_die(head, sha, repo, source=SOURCE_CI, ref=value)

    # 2. QUALITY_BASE_REF from .quality/config.toml.
    if config.base_ref:
        sha = gitutil.resolve_to_sha(config.base_ref, repo)
        if sha is None:
            raise BaseResolutionError(
                f"base ref {config.base_ref!r} from .quality/config.toml does "
                "not resolve to a commit",
                remedy="Point [base] ref at an existing branch or SHA.",
            )
        return _merge_base_or_die(head, sha, repo, source=SOURCE_CONFIG, ref=config.base_ref)

    # 3. remote HEAD of each remote (deterministic order).
    remotes_proc = gitutil.git("remote", cwd=repo)
    remotes = sorted(r for r in remotes_proc.stdout.split() if r)
    for remote in remotes:
        sym = gitutil.git(
            "symbolic-ref", "--quiet", f"refs/remotes/{remote}/HEAD",
            cwd=repo, check=False,
        )
        ref = sym.stdout.strip() if sym.returncode == 0 else ""
        if not ref:
            continue
        sha = gitutil.resolve_to_sha(ref, repo)
        if sha is not None:
            return _merge_base_or_die(head, sha, repo, source=SOURCE_REMOTE_HEAD, ref=ref)

    raise BaseResolutionError(
        "could not resolve a base reference (no CI variable, no config "
        "[base] ref, no remote HEAD)",
        remedy="Set the CI base variable, or add [base] ref = \"<branch>\" "
               "to .quality/config.toml (v5.1 §4.6).",
    )
