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

# Base-resolution reads these before [base] ref (v5.1 §4.6), so a real
# CI sets GITHUB_BASE_REF on every pull_request run. Tests must not see
# the host's values — neither in-process nor in scratch CLI runs.
CI_BASE_VARS = (
    "GITHUB_BASE_SHA",
    "GITHUB_BASE_REF",
    "CI_MERGE_REQUEST_DIFF_BASE_SHA",
    "CI_MERGE_REQUEST_TARGET_BRANCH_NAME",
    "QUALITY_BASE_REF",
)


@pytest.fixture(autouse=True)
def no_ci_base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide the host CI's base variables from every in-process test.

    run_cli blanks them for scratch subprocesses; without this fixture
    the same variables leak into tests that call resolve_base directly,
    and the suite fails on any pull_request event (where GitHub always
    sets GITHUB_BASE_REF).
    """
    for var in CI_BASE_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(scope="session", autouse=True)
def prewarm_scratch_envs(test_cache: Path) -> None:
    """Build the scratch analyzer env before the first test runs.

    The SCRATCH_TOOLCHAIN bytes every scratch repo writes hash to the
    same content-keyed env, so building it here (plus the default
    scratch project env) turns the first CLI run's multi-minute cold
    install into a cache hit instead of a first-test timeout risk.
    Under pytest-xdist each worker runs this fixture; the O_EXCL build
    lock in aufsicht.toolchain serializes the build while the other
    workers poll. A network failure surfaces as a session-scoped error
    — accepted (CI-SPEED-PLAN §4, Milestone 2.1).
    """
    import tempfile

    from aufsicht import toolchain
    from tests.fixtures.scratch import SCRATCH_PYPROJECT, SCRATCH_TOOLCHAIN

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / ".quality").mkdir()
        # Byte-exact like install_quality: the env key is the lock's
        # bytes (a text-mode write would translate line endings on
        # Windows and build a second env); the pyproject mirrors
        # write_files' text write so the project-env key matches.
        (root / ".quality" / "toolchain.lock").write_bytes(SCRATCH_TOOLCHAIN.encode("utf-8"))
        (root / "pyproject.toml").write_text(SCRATCH_PYPROJECT, encoding="utf-8")
        lock = toolchain.load_toolchain(root)
        toolchain.analyzer_env(lock, TEST_CACHE)
        toolchain.project_env(root, lock, TEST_CACHE)


def run_git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, **GIT_ENV}
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env=env)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {args} failed: {proc.stderr}")
    return proc


def run_cli(*args: str, cwd: Path, timeout: int = 1200) -> subprocess.CompletedProcess:
    """Invoke the real CLI: `python -m aufsicht <args>` in *cwd*."""
    env = {
        **os.environ,
        # The CLI commits (`aufsicht init --write`) with the same
        # identity as the harness, hermetically: CI runners have no
        # global git identity, and their hostname makes git's
        # auto-detected email unusable, so the inner `git commit`
        # would fail there while passing on a developer machine.
        **GIT_ENV,
        "AUFSICHT_CACHE_DIR": str(TEST_CACHE),
        # No CI variables leak into scratch runs; base resolution goes
        # through [base] ref in the scratch config.
        **{k: "" for k in CI_BASE_VARS},
    }
    return subprocess.run(
        [sys.executable, "-m", "aufsicht", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


@pytest.fixture(scope="session")
def test_cache() -> Path:
    TEST_CACHE.mkdir(parents=True, exist_ok=True)
    return TEST_CACHE
