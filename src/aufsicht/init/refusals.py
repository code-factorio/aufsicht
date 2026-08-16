"""Refusals (distribution spec §5.4). Each exits 1 with the specific
remedy printed. None is auto-resolved — the repository needs a human
decision first.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from ..errors import RefusalError

# Sections of pyproject.toml that are quality policy (v5.1 §11.2): all
# quality configuration lives outside pyproject.toml, which is also
# what makes path-level protection expressible.
PYPROJECT_QUALITY_SECTIONS = (
    "tool.ruff",
    "tool.pytest.ini_options",
    "tool.deptry",
    "tool.mutmut",
    "tool.coverage.run",
    "tool.coverage.report",
)


def check_refusals(repo: Path) -> None:
    """Raise RefusalError for any §5.4 condition. In order."""
    from .. import gitutil

    if not gitutil.is_git_repo(repo):
        raise RefusalError(
            f"{repo} is not a git repository",
            remedy="aufsicht guards existing git repositories (v5.1 §2; "
                   "no non-git VCS). Initialise git first.",
        )

    if (repo / ".quality").exists():
        raise RefusalError(
            ".quality/ is already present — this repository already has "
            "guardrails installed",
            remedy="Re-initialisation is a guardrail change: it goes "
                   "through review like any other (v5.1 §11.1). To "
                   "overwrite deliberately, run `aufsicht init --force` "
                   "on a clean tree.",
        )

    offending = pyproject_quality_sections(repo / "pyproject.toml")
    if offending:
        raise RefusalError(
            "quality configuration found in pyproject.toml: "
            + ", ".join(f"[{s}]" for s in offending),
            remedy="Move these sections out of pyproject.toml per v5.1 "
                   "§11.2 (`git diff` cannot see TOML sections, so "
                   "protection is not expressible there). Delete them "
                   "from pyproject.toml; `aufsicht init` will place the "
                   "equivalent configuration under .quality/. "
                   "Specifically: "
                   + " ".join(f"[{s}]" for s in offending),
        )

    if gitutil.is_dirty(repo):
        raise RefusalError(
            "working tree is dirty",
            remedy="Commit or stash your changes first — init writes a "
                   "branch with the installation and must not mix with "
                   "uncommitted work.",
        )

    if gitutil.is_shallow(repo):
        raise RefusalError(
            "repository is a shallow clone — ratchets need a real merge "
            "base (v5.1 §4.6)",
            remedy="Fetch full history: `git fetch --unshallow` locally, "
                   "and use fetch-depth: 0 in CI.",
        )

    if (repo / ".github" / "workflows" / "aufsicht.yml").exists():
        raise RefusalError(
            ".github/workflows/aufsicht.yml already exists",
            remedy="init never merges into an existing CI workflow "
                   "(distribution spec §5.5). Move or rename the "
                   "existing file, or wire aufsicht into it by hand.",
        )


def force_refusals(repo: Path) -> None:
    """Refusals that apply even with --force (§5.3: --force refuses on a
    dirty tree; and the structural ones that make no sense to bypass)."""
    from .. import gitutil

    if not gitutil.is_git_repo(repo):
        raise RefusalError(
            f"{repo} is not a git repository",
            remedy="aufsicht guards existing git repositories.",
        )
    if gitutil.is_dirty(repo):
        raise RefusalError(
            "working tree is dirty",
            remedy="--force still refuses a dirty tree: commit or stash "
                   "first.",
        )
    if gitutil.is_shallow(repo):
        raise RefusalError(
            "repository is a shallow clone (v5.1 §4.6)",
            remedy="git fetch --unshallow, and fetch-depth: 0 in CI.",
        )


def pyproject_quality_sections(pyproject: Path) -> list[str]:
    if not pyproject.is_file():
        return []
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return []
    found: list[str] = []
    for section in PYPROJECT_QUALITY_SECTIONS:
        node: object = data
        ok = True
        for part in section.split("."):
            if not isinstance(node, dict) or part not in node:
                ok = False
                break
            node = node[part]
        if ok:
            found.append(section)
    return found
