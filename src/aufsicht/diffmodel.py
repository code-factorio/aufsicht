"""Parse ``git diff -U0`` into the DiffModel (v5.1 §4.2).

``added_lines`` comes from hunk headers, per file. The comparison is
base-commit → working tree (tracked changes) plus untracked files,
because the fast loop runs on the developer's uncommitted work and a
finding in a brand-new file is definitionally new.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import gitutil
from .errors import ToolingError
from .model import DiffModel

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _strip_prefix(path: str) -> str:
    if path.startswith("b/"):
        return path[2:]
    return path


def parse_unified_diff(text: str) -> tuple[set[str], dict[str, list[tuple[int, int]]]]:
    """Parse `git diff -U0` output into (changed paths, added ranges)."""
    changed: set[str] = set()
    added: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("+++ b/"):
            current = _strip_prefix(line[4:])
            changed.add(current)
        elif line.startswith("+++ /dev/null"):
            current = None
        elif line.startswith("@@") and current is not None:
            m = _HUNK.match(line)
            if not m:
                raise ToolingError(f"unparseable diff hunk header: {line!r}")
            start = int(m.group(1))
            count = int(m.group(2) or "1")
            if count > 0:
                added.setdefault(current, []).append((start, count))
    return changed, added


def build_diff(repo: Path, base_sha: str) -> DiffModel:
    """Diff of *base_sha* → working tree, plus untracked files.

    ``--no-renames`` so a rename surfaces as deletion + addition: a
    100%-similarity rename of a protected path (or of a violating
    module) must not slip past the diff model. Deleted files are part
    of ``changed_files`` — deleting AGENTS.md or .quality/allowlist.toml
    is a protected-path change the integrity gate must see, and the -U0
    hunk parser alone would drop it (its ``+++ /dev/null`` side carries
    no added lines).
    """
    proc = gitutil.git(
        "diff", "-U0", "--no-color", "--no-ext-diff", "--no-renames",
        base_sha, cwd=repo,
    )
    changed, added = parse_unified_diff(proc.stdout)

    # Name-only union: guarantees deletions are present even though
    # they contribute no added lines.
    for path in gitutil.changed_paths(base_sha, repo):
        changed.add(path)

    # Untracked files: entirely new, so every line is an added line.
    for path in gitutil.untracked_files(repo):
        changed.add(path)
        full = repo / path
        try:
            n_lines = sum(1 for _ in full.open("rb"))
        except OSError:
            n_lines = 0
        if n_lines:
            added.setdefault(path, []).append((1, n_lines))

    return DiffModel(changed_files=frozenset(changed), added_lines=added)
