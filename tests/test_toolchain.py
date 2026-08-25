"""Analyzer-env cache-hit verification self-tests (CI-SPEED-PLAN M3d).

`_env_complete` must not trust the `.aufsicht-ok` marker alone: an env
restored from a CI cache with the marker intact but wrong tool versions
installed would silently run the ratchet on diagnostics from the wrong
analyzer versions. A failed verification is a rebuild — never a pass,
never a silent skip.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

import aufsicht.toolchain as tc
from tests.fixtures.scratch import SCRATCH_TOOLCHAIN


def write_layout(env: Path, versions: dict[str, str], entry_points=("ruff",)) -> None:
    """Materialize the parts of a venv that verification reads: dist-info
    directory names and bin scripts with live shebangs (no marker)."""
    site = env / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    for name, version in versions.items():
        (site / f"{name.replace('-', '_')}-{version}.dist-info").mkdir()
    bin_dir = env / "bin"
    bin_dir.mkdir(exist_ok=True)
    for tool in entry_points:
        script = bin_dir / tool
        script.write_text(f"#!{sys.executable}\n", encoding="utf-8")
        script.chmod(0o755)


def make_env(root: Path, versions: dict[str, str], entry_points=("ruff",)) -> Path:
    """A marker-valid env skeleton (the state a restored cache presents)."""
    env = root / "env"
    write_layout(env, versions, entry_points)
    (env / ".aufsicht-ok").write_text("built-by-aufsicht\n", encoding="utf-8")
    return env


class TestEnvComplete:
    def test_marker_and_correct_versions_are_complete(self, tmp_path):
        env = make_env(
            tmp_path,
            {"ruff": "0.16.3", "pip-audit": "2.10.1"},
            entry_points=("ruff", "pip-audit"),
        )
        pins = {"ruff": "0.16.3", "pip-audit": "2.10.1"}
        assert tc._env_complete(env, ["ruff", "pip-audit"], pins)

    def test_wrong_version_is_incomplete(self, tmp_path):
        # Marker present, entry point live — only the version is wrong.
        env = make_env(tmp_path, {"ruff": "0.16.2"})
        assert not tc._env_complete(env, ["ruff"], {"ruff": "0.16.3"})

    def test_absent_distribution_is_incomplete(self, tmp_path):
        env = make_env(tmp_path, {"ruff": "0.16.3"})
        assert not tc._env_complete(env, ["ruff"], {"ruff": "0.16.3", "semgrep": "1.173.0"})

    def test_plugin_only_tool_is_version_checked(self, tmp_path):
        # pytest-cov ships no console script; its pin is still verified.
        env = make_env(
            tmp_path, {"pytest": "9.1.1", "pytest-cov": "7.0.0"}, entry_points=("pytest",)
        )
        pins = {"pytest": "9.1.1", "pytest-cov": "7.0.0"}
        assert tc._env_complete(env, ["pytest"], pins)
        assert not tc._env_complete(env, ["pytest"], {"pytest": "9.1.1", "pytest-cov": "7.0.1"})

    def test_duplicate_distributions_are_incomplete(self, tmp_path):
        # Leftovers of an in-place upgrade: two ruffs match no exact pin.
        env = make_env(tmp_path, {"ruff": "0.16.3"})
        site = env / "lib" / "python3.12" / "site-packages"
        (site / "ruff-0.16.4.dist-info").mkdir()
        assert not tc._env_complete(env, ["ruff"], {"ruff": "0.16.3"})

    def test_unpinned_fallback_has_nothing_to_mismatch(self, tmp_path):
        # `pytest` without a pin installs whatever resolves; no expected
        # version exists to compare against.
        env = make_env(tmp_path, {"pytest": "9.9.9"}, entry_points=("pytest",))
        assert tc._env_complete(env, ["pytest"], {})


class TestVersionMismatches:
    def test_reports_name_and_both_versions(self, tmp_path):
        env = make_env(tmp_path, {"ruff": "0.16.2"})
        assert tc._version_mismatches(env, {"ruff": "0.16.3"}) == ["ruff: '0.16.2' != '0.16.3'"]

    def test_absent_distribution_reported_as_none(self, tmp_path):
        env = make_env(tmp_path, {"ruff": "0.16.3"})
        assert tc._version_mismatches(env, {"semgrep": "1.173.0"}) == ["semgrep: None != '1.173.0'"]

    def test_empty_for_a_matching_env(self, tmp_path):
        env = make_env(tmp_path, {"ruff": "0.16.3"})
        assert tc._version_mismatches(env, {"ruff": "0.16.3"}) == []


class TestRebuildOnMismatch:
    def test_wrong_version_triggers_rebuild(self, tmp_path):
        env = make_env(tmp_path, {"ruff": "0.16.2"})
        built = []

        def build(target: Path) -> None:
            built.append(target)
            write_layout(target, {"ruff": "0.16.3"})

        result = tc._build_env_in_place(env, ["ruff"], {"ruff": "0.16.3"}, build)
        assert built == [env]
        assert result == env
        assert tc._env_complete(env, ["ruff"], {"ruff": "0.16.3"})

    def test_matching_env_is_not_rebuilt(self, tmp_path):
        env = make_env(tmp_path, {"ruff": "0.16.3"})
        built = []

        def build(target: Path) -> None:
            built.append(target)

        assert tc._build_env_in_place(env, ["ruff"], {"ruff": "0.16.3"}, build) == env
        assert built == []


@pytest.fixture(scope="module")
def pinned_lock_and_env(tmp_path_factory):
    """The shared warm analyzer env for the scratch pins (same pattern
    as tests/test_global_probes.py: one env per machine, keyed on the
    lock hash)."""
    tmp = tmp_path_factory.mktemp("toolchain-locks")
    (tmp / ".quality").mkdir()
    (tmp / ".quality" / "toolchain.lock").write_text(SCRATCH_TOOLCHAIN)
    lock = tc.load_toolchain(tmp)
    return lock, tc.analyzer_env(lock)


def skeleton_copy(src: Path, dst: Path) -> Path:
    """Copy only what a cache-hit verification reads from *src*: bin
    scripts, dist-info directory names (as symlinks — the scan reads
    names, not contents), and the trust marker."""
    site_src = next((src / "lib").glob("python*")) / "site-packages"
    site_dst = dst / "lib" / site_src.parent.name / "site-packages"
    site_dst.mkdir(parents=True)
    for entry in site_src.iterdir():
        if entry.name.endswith(".dist-info"):
            (site_dst / entry.name).symlink_to(entry)
    (dst / "bin").mkdir(parents=True)
    for entry in (src / "bin").iterdir():
        if entry.is_symlink():
            continue  # interpreters: entry-point shebangs point at the real env
        if entry.is_file():
            shutil.copy2(entry, dst / "bin" / entry.name)
    (dst / ".aufsicht-ok").write_text("built-by-aufsicht\n", encoding="utf-8")
    return dst


class TestWarmEnvVerification:
    def test_warm_analyzer_env_matches_the_lock(self, pinned_lock_and_env):
        # The real cached env verifies complete against the real lock:
        # the warm-cache gate run must not rebuild (M3d acceptance).
        lock, env = pinned_lock_and_env
        pins = dict(lock.tools)
        entry_points = [n for n in sorted(pins) if n not in tc.PLUGIN_ONLY_TOOLS]
        assert tc._env_complete(env, entry_points, pins)

    def test_wrong_version_in_restored_copy_triggers_rebuild(self, pinned_lock_and_env, tmp_path):
        # The ticket scenario: a cache restore that leaves the marker
        # and every entry point intact but one tool at the wrong
        # version. Marker and scripts look fine; only versions tell.
        lock, env = pinned_lock_and_env
        pins = dict(lock.tools)
        entry_points = [n for n in sorted(pins) if n not in tc.PLUGIN_ONLY_TOOLS]
        restored = skeleton_copy(env, tmp_path / "restored")
        site = next((restored / "lib").glob("python*")) / "site-packages"
        (site / "ruff-0.16.3.dist-info").rename(site / "ruff-0.16.2.dist-info")
        assert not tc._env_complete(restored, entry_points, pins)

        built = []

        def build(target: Path) -> None:
            built.append(target)
            write_layout(target, pins, entry_points)

        tc._build_env_in_place(restored, entry_points, pins, build)
        assert built == [restored]


class TestStaleLockTakeover:
    def test_stale_lock_is_reacquired_and_built(self, tmp_path, monkeypatch):
        # A lock left behind by a dead builder must not cost a full
        # wait_timeout of polling: the waiter hands the stale lock back
        # and the caller re-acquires and builds (observed live: a
        # killed process left proj-*.lock behind and the next build of
        # that env polled for an hour before failing).
        import os
        import time

        monkeypatch.setattr(tc, "_ENV_BUILD_POLL_SECONDS", 0.01)
        env = tmp_path / "envs" / "analyzer-stale"
        env.parent.mkdir(parents=True)
        lock = env.with_name(env.name + ".lock")
        lock.write_text("999999", encoding="utf-8")  # no such process
        os.utime(lock, (0, 0))  # far past any staleness threshold

        built = []

        def build(target: Path) -> None:
            built.append(target)
            write_layout(target, {"ruff": "0.16.3"})

        started = time.monotonic()
        result = tc._build_env_in_place(
            env,
            ["ruff"],
            {"ruff": "0.16.3"},
            build,
            lock_stale_seconds=0.05,
            wait_timeout=30.0,
        )
        assert result == env
        assert built == [env]
        assert time.monotonic() - started < 10  # no wait_timeout burn
        assert not lock.exists()
