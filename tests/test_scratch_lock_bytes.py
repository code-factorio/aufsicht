"""Fixture invariant: the scratch toolchain lock is the repo lock.

The analyzer-env cache key is sha256 of the lock bytes
(`aufsicht.toolchain.analyzer_env`), so the scratch lock must equal
`.quality/toolchain.lock` byte for byte. Any drift silently hands every
scratch run a second analyzer environment — one more cold build, one
more cache entry (CI-SPEED-PLAN.md Milestone 2.2).

This lives in its own file rather than next to the other lock
assertions in test_audit_fixes.py because that module still carries
pre-existing lint/format findings and the ruff gate is changed-file
scoped: touching it would pin that debt on an unrelated PR.
"""

from __future__ import annotations

from tests.conftest import REPO_ROOT
from tests.fixtures.scratch import SCRATCH_TOOLCHAIN


def test_scratch_lock_is_byte_identical_to_repo_lock():
    repo_lock = (REPO_ROOT / ".quality" / "toolchain.lock").read_bytes()
    assert SCRATCH_TOOLCHAIN.encode("utf-8") == repo_lock, (
        "SCRATCH_TOOLCHAIN drifted from .quality/toolchain.lock — "
        "scratch runs would build a second analyzer environment "
        "(CI-SPEED-PLAN.md Milestone 2.2)"
    )
