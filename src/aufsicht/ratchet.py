"""Per-rule count ratchet (v5.1 §4.3) and its machinery.

    for each rule r in (the union of BASE.keys and HEAD.keys):
        HEAD[r] <= BASE[r]   → pass
        HEAD[r] >  BASE[r]   → fail, reporting r and both integers

Null rule ids are bucketed under "<no-rule>" and ratcheted like any
other. BASE source is analysed under BASE's tool configuration, HEAD
source under HEAD's; where the two configurations genuinely differ,
§4.5's exemption handles the incomparability.

Base counts are computed from a `git worktree` at the merge base at
gate time — never committed (v5.1 §4.3, "not an artifact") — and cached
keyed on the base commit SHA so ratcheted gates do not run every tool
twice per invocation.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import gitutil
from .config import cache_dir
from .errors import ToolingError
from .model import NO_RULE

# base_worktree() lock discipline: poll interval for waiters, how long
# to wait for another process's `git worktree add` (seconds of wall
# clock), and how old a lock file may be before it is treated as left
# behind by a dead holder.
_WORKTREE_POLL_SECONDS = 0.1
_WORKTREE_WAIT_SECONDS = 600.0
_WORKTREE_STALE_SECONDS = 60.0


def bucket(rule: str | None) -> str:
    """Normalise a rule id to its ratchet bucket."""
    return rule if rule else NO_RULE


def count_by_rule(rules: list[str | None]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rule in rules:
        counts[bucket(rule)] = counts.get(bucket(rule), 0) + 1
    return counts


@dataclass(frozen=True)
class RegressedRule:
    rule: str
    base: int
    head: int

    def to_dict(self) -> dict:
        return {"rule": self.rule, "base": self.base, "head": self.head}


@dataclass
class RatchetOutcome:
    regressed: list[RegressedRule] = field(default_factory=list)
    base_counts: dict[str, int] = field(default_factory=dict)
    head_counts: dict[str, int] = field(default_factory=dict)

    @property
    def totals(self) -> tuple[int, int]:
        return (sum(self.base_counts.values()), sum(self.head_counts.values()))

    @property
    def passed(self) -> bool:
        return not self.regressed

    def to_dict(self) -> dict:
        base_total, head_total = self.totals
        return {
            "regressed_rules": [r.to_dict() for r in self.regressed],
            "totals": {"base": base_total, "head": head_total},
        }


def compare(base_counts: dict[str, int], head_counts: dict[str, int]) -> RatchetOutcome:
    """Bucket-by-bucket comparison (v5.1 §4.3).

    A rule present in BASE and absent from HEAD satisfies the ratchet
    naturally (HEAD[rule] = 0 <= BASE[rule]) — no special handling.
    """
    regressed: list[RegressedRule] = []
    for rule in sorted(set(base_counts) | set(head_counts)):
        base = base_counts.get(rule, 0)
        head = head_counts.get(rule, 0)
        if head > base:
            regressed.append(RegressedRule(rule=rule, base=base, head=head))
    return RatchetOutcome(
        regressed=regressed, base_counts=dict(base_counts), head_counts=dict(head_counts)
    )


# --- base worktrees -------------------------------------------------------


def base_worktree(repo: Path, sha: str, cache: Path | None = None) -> Path:
    """A persistent `git worktree` checked out at *sha*.

    Lives under the aufsicht cache, keyed on the SHA, so repeated gates
    reuse one checkout. The BASE worktree contributes source and BASE's
    own config files — never analyzer versions (v5.1 §4.4).

    The check-and-create is guarded by an exclusive lock file keyed on
    the SHA (the advisory pattern toolchain.py uses for env builds):
    concurrent callers with the same base SHA — ratcheted gates in one
    run, or xdist workers whose scratch repos share a deterministic
    initial commit — wait for the winner instead of racing
    `git worktree add` into "already exists" (exit 3, fail closed).
    """
    cache = cache or cache_dir()
    wt = cache / "worktrees" / sha
    if (wt / ".git").exists():
        return wt
    wt.parent.mkdir(parents=True, exist_ok=True)
    lock = wt.parent / f"{sha}.lock"
    deadline = time.monotonic() + _WORKTREE_WAIT_SECONDS
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            _take_over_stale_lock(lock)
            if (wt / ".git").exists():
                return wt
            if time.monotonic() > deadline:
                raise ToolingError(
                    f"timed out waiting for the base worktree at {sha}: {lock} still held",
                    remedy="Another process is creating the worktree; check "
                    "for a stuck git process or remove the stale "
                    "lock file.",
                ) from None
            time.sleep(_WORKTREE_POLL_SECONDS)
            continue
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(str(os.getpid()))
            if not (wt / ".git").exists():
                _add_worktree(repo, wt, sha)
        finally:
            lock.unlink(missing_ok=True)
        return wt


def _add_worktree(repo: Path, wt: Path, sha: str) -> None:
    proc = gitutil.git("worktree", "add", "--detach", str(wt), sha, cwd=repo, check=False)
    if proc.returncode != 0:
        # A stale registration from a deleted cache, most likely.
        gitutil.git("worktree", "prune", cwd=repo, check=False)
        proc = gitutil.git("worktree", "add", "--detach", str(wt), sha, cwd=repo, check=False)
        if proc.returncode != 0:
            raise ToolingError(
                f"could not create worktree at {sha}: {proc.stderr.strip()}",
                remedy="Check that the base commit is reachable and the "
                "cache directory is writable.",
            )


def _take_over_stale_lock(lock: Path) -> None:
    try:
        age = time.time() - lock.stat().st_mtime
    except FileNotFoundError:
        return
    if age > _WORKTREE_STALE_SECONDS:
        # The holder died mid-create; take the lock over. The next
        # O_EXCL attempt by this (or any) caller wins it.
        lock.unlink(missing_ok=True)


# --- base-run cache -------------------------------------------------------


def _cache_file(key_material: dict | list, cache: Path) -> Path:
    blob = json.dumps(key_material, sort_keys=True).encode()
    key = hashlib.sha256(blob).hexdigest()
    d = cache / "ratchet-cache"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.json"


def cache_key(*, base_sha: str, tool: str, lock_hash: str, config_hash: str) -> list:
    """Key on everything that changes what a base run would produce:
    the commit, the analyzer pin set, and BASE's configuration for the
    tool (§4.3: BASE source is analysed under BASE's configuration)."""
    return [base_sha, tool, lock_hash, config_hash]


def cached_base_counts(key: list, cache: Path | None, produce) -> dict[str, int]:
    """Load cached base counts for *key*, computing them via *produce* on
    miss. Counts are cached, never committed (v5.1 §4.3)."""
    cache = cache or cache_dir()
    path = _cache_file(key, cache)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass  # corrupt cache entry: recompute
    counts = produce()
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(counts, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # cache is an optimisation, never a gate input
    return counts


def read_file_at(repo: Path, sha: str, path: str) -> bytes | None:
    """Contents of *path* at *sha*, or None when it did not exist."""
    proc = gitutil.git("show", f"{sha}:{path}", cwd=repo, check=False, text=False)
    if proc.returncode != 0:
        return None
    return proc.stdout
