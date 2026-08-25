"""Analyzer-env cache-hit verification self-tests (CI-SPEED-PLAN M3d).

`_env_complete` must not trust the `.aufsicht-ok` marker alone: an env
restored from a CI cache with the marker intact but wrong tool versions
installed would silently run the ratchet on diagnostics from the wrong
analyzer versions. A failed verification is a rebuild — never a pass,
never a silent skip.

The project_env tests cover issue #19: uv is required to BUILD a
project dependency environment (a pins-only env would make the pytest
gate report the broken environment as test findings), while a
cached-complete env verifies without a build and so needs no uv; and
the pins-only retry after a failed `-r pyproject.toml` install must
drop the requirement file, not rerun the identical command.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import aufsicht.toolchain as tc
from tests.fixtures.scratch import SCRATCH_PYPROJECT, SCRATCH_TOOLCHAIN


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


def proj_repo(root: Path, pyproject: str = SCRATCH_PYPROJECT) -> tuple[Path, tc.Toolchain]:
    """A minimal repo for project_env: the scratch pins plus whatever
    *pyproject* the test needs (the default declares an installable,
    empty-dependency [project] table)."""
    repo = root / "repo"
    (repo / ".quality").mkdir(parents=True)
    (repo / ".quality" / "toolchain.lock").write_text(SCRATCH_TOOLCHAIN, encoding="utf-8")
    (repo / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    return repo, tc.load_toolchain(repo)


def proj_env_path(cache: Path, repo: Path, lock: tc.Toolchain) -> Path:
    """The exact env directory project_env resolves *repo* to — the
    cache-key derivation mirrored for tests that pre-materialize it."""
    pins = tc.project_env_pins(lock)
    files_key, _lockfile = tc.project_env_key(repo)
    pin_key = hashlib.sha256("\n".join(pins).encode()).hexdigest()[:12]
    return cache / "projenvs" / f"proj-{files_key}-{pin_key}"


class TestProjectEnvRequiresUv:
    def test_cold_cache_without_uv_is_a_tooling_error(self, tmp_path, monkeypatch):
        # Issue #19: without uv there is no pins-only env — the pytest
        # gate would report the missing project dependencies as test
        # findings, i.e. a broken environment as defective project code.
        # A defective environment is a tooling error (exit 3).
        monkeypatch.setattr(tc, "_have_uv", lambda: False)
        repo, lock = proj_repo(tmp_path)
        with pytest.raises(tc.ToolingError, match="uv is required") as excinfo:
            tc.project_env(repo, lock, tmp_path / "cache")
        assert "Install uv" in (excinfo.value.remedy or "")

    def test_warm_cache_without_uv_is_exempt(self, tmp_path, monkeypatch):
        # The requirement lives inside build(), so a cached-complete env
        # verifies and returns without ever building: a warm cache works
        # without uv, and only a build that is needed but impossible
        # fails. The placement is the point of this test.
        monkeypatch.setattr(tc, "_have_uv", lambda: False)
        repo, lock = proj_repo(tmp_path)
        cache = tmp_path / "cache"
        pins = tc.project_env_pins(lock)
        env = proj_env_path(cache, repo, lock)
        write_layout(env, dict(tc._pin_versions(list(pins))), entry_points=tc._entry_points(pins))
        (env / ".aufsicht-ok").write_text("built-by-aufsicht\n", encoding="utf-8")
        assert tc.project_env(repo, lock, cache) == env


class TestProjectEnvPinsOnlyRetry:
    def test_pyproject_without_project_table_still_builds(self, tmp_path):
        # The real fallback path (issue #19), not mocked: a pyproject
        # with no [project] table fails `uv pip install -r
        # pyproject.toml` with a metadata error. The retry must drop the
        # requirement file and run on the pins — the gate still gets its
        # runner, never a half-resolved environment.
        repo, lock = proj_repo(tmp_path, pyproject='[tool.demo]\nkey = "value"\n')
        env = tc.project_env(repo, lock, tmp_path / "cache")
        assert (tc._bin_dir(env) / "pytest").exists()

    def test_retry_command_drops_the_pyproject_requirement(self, tmp_path, monkeypatch):
        # Command shape of the same path (the slice bug): the old
        # `install[:-len(pins)]` cut only the pins and reran the
        # identical `-r pyproject.toml` command. The retry must equal
        # base + pins, with neither "-r" nor "pyproject.toml" in it.
        repo, lock = proj_repo(tmp_path)
        cache = tmp_path / "cache"
        pins = tc.project_env_pins(lock)
        env = proj_env_path(cache, repo, lock)
        commands: list[list[str]] = []

        def fake_subprocess_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
            commands.append(list(cmd))
            return SimpleNamespace(returncode=1, stdout="", stderr="simulated -r failure")

        def fake_run(cmd: list[str], **_kwargs: object) -> None:
            commands.append(list(cmd))
            if cmd[1] == "pip":  # the pins-only retry: materialize the env
                write_layout(
                    env, dict(tc._pin_versions(list(pins))), entry_points=tc._entry_points(pins)
                )

        monkeypatch.setattr(tc.subprocess, "run", fake_subprocess_run)
        monkeypatch.setattr(tc, "_run", fake_run)
        assert tc.project_env(repo, lock, cache) == env

        base = ["uv", "pip", "install", "--quiet", "--python", str(tc._bin_dir(env) / "python")]
        assert commands[1] == [*base, "-r", "pyproject.toml", *list(pins)]
        retry = commands[2]
        assert "-r" not in retry
        assert "pyproject.toml" not in retry
        assert retry == [*base, *list(pins)]
