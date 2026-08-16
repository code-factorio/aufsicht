"""Shared test harness.

Self-tests are generated scratch repositories, not mocks (distribution
spec §8): real git history, a real merge base, a real violation
applied, the real CLI invoked.

The analyzer environment is heavy (semgrep et al.), so the runtime
cache points at a machine-stable directory — built once, reused across
sessions. Env creation is keyed on the toolchain lock hash, so
different scratch repos with the same pins share one env.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "templates"

# Stable across sessions so analyzer envs are built once per machine.
TEST_CACHE = Path(os.environ.get("AUFSICHT_TEST_CACHE", "/tmp/aufsicht-test-cache"))
os.environ.setdefault("AUFSICHT_CACHE_DIR", str(TEST_CACHE))

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Scratch Tester",
    "GIT_AUTHOR_EMAIL": "scratch@example.invalid",
    "GIT_COMMITTER_NAME": "Scratch Tester",
    "GIT_COMMITTER_EMAIL": "scratch@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def run_git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, **GIT_ENV}
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, env=env
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {args} failed: {proc.stderr}")
    return proc


def run_cli(
    *args: str, cwd: Path, timeout: int = 1200
) -> subprocess.CompletedProcess:
    """Invoke the real CLI: `python -m aufsicht <args>` in *cwd*."""
    env = {
        **os.environ,
        "AUFSICHT_CACHE_DIR": str(TEST_CACHE),
        # No CI variables leak into scratch runs; base resolution goes
        # through [base] ref in the scratch config.
        **{k: "" for k in (
            "GITHUB_BASE_SHA", "GITHUB_BASE_REF",
            "CI_MERGE_REQUEST_DIFF_BASE_SHA",
            "CI_MERGE_REQUEST_TARGET_BRANCH_NAME",
            "QUALITY_BASE_REF",
        )},
    }
    return subprocess.run(
        [sys.executable, "-m", "aufsicht", *args],
        cwd=str(cwd), capture_output=True, text=True, env=env, timeout=timeout,
    )


@pytest.fixture(scope="session")
def test_cache() -> Path:
    TEST_CACHE.mkdir(parents=True, exist_ok=True)
    return TEST_CACHE
