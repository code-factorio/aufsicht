"""Self-tests for the pytest gate (v5.1 §18 "test failure" row, §5).

Each CLI case is a generated scratch repository with a real merge base
and the real `python -m aufsicht` invoked (distribution spec §8). The
parser/selection cases run against the exact output shapes measured
from the pinned pytest 9.1.1 (see the adapter's module docstring).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from aufsicht import pipeline
from aufsicht.adapters import pytest_adapter
from aufsicht.adapters.pytest_adapter import (
    affected_test_files,
    changed_src_modules,
    discover_test_files,
    parse_summary,
    run_suite,
)
from aufsicht.base import BaseRef
from aufsicht.config import QualityConfig
from aufsicht.model import DiffModel
from aufsicht.report import Report
from aufsicht.toolchain import load_toolchain, project_env, project_env_pins
from tests.conftest import run_cli, run_git
from tests.fixtures.scratch import commit, make_repo

FAILING_TEST = """\
from scratch.app import add


def test_add_wrong():
    assert add(2, 3) == 6
"""

BROKEN_TEST = (
    # Module-level raise: the test module fails at collection (import)
    # time. A literal syntax error would abort the *ruff* gate first
    # with a parse ToolingError (exit 2), which is ruff's semantics,
    # not this gate's — an import-time exception isolates the pytest
    # collection-error path.
    "from scratch.app import add\n"
    "\n"
    '_PRELUDE = int("not a number")\n'
    "\n"
    "\n"
    "def test_never_collected():\n"
    "    assert add(1, 2) == 3\n"
)


def run_quality(mode: str, repo: Path) -> tuple[int, dict]:
    proc = run_cli(mode, cwd=repo)
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"quality-{mode} produced no JSON report.\n"
            f"stdout: {proc.stdout[:500]}\nstderr: {proc.stderr[:2000]}"
        ) from exc
    return proc.returncode, report


def pytest_gate(report: dict) -> dict:
    entry = report["gates"].get("pytest")
    assert entry is not None, f"pytest gate missing from report: {list(report['gates'])}"
    return entry


class TestFullMode:
    def test_failing_test_fails_gate_absolute(self, tmp_path):
        # v5.1 §18: a failing test → pytest gate, absolute, exit 1,
        # one finding per failed test with path and test id.
        repo = make_repo(tmp_path / "fail")
        commit(repo, {"tests/test_app.py": FAILING_TEST}, "failing test", branch="feature")
        code, report = run_quality("full", repo)
        assert code == 1, json.dumps(report, indent=2)
        assert report["status"] == "fail"
        entry = pytest_gate(report)
        assert entry["status"] == "fail", entry
        assert entry["mechanism"] == "absolute"
        findings = [f for f in report["findings"] if f["gate"] == "pytest"]
        assert findings, entry
        assert findings[0]["rule"] == "pytest/failure"
        assert findings[0]["path"] == "tests/test_app.py"
        assert findings[0]["symbol"] == "test_add_wrong"
        assert "6" in findings[0]["detail"]
        assert "5" in findings[0]["detail"]

    def test_clean_suite_passes(self, tmp_path):
        repo = make_repo(tmp_path / "clean")
        code, report = run_quality("full", repo)
        assert code == 0, json.dumps(report, indent=2)
        entry = pytest_gate(report)
        assert entry["status"] == "pass", entry
        assert entry["mechanism"] == "absolute"
        assert isinstance(entry.get("suite_seconds"), (int, float))

    def test_no_tests_in_repository_skips_honestly(self, tmp_path):
        # Exit 5 (no tests collected) is neither a failure nor an error.
        repo = make_repo(tmp_path / "notests")
        (repo / "tests" / "test_app.py").unlink()
        commit(repo, {}, "remove the only test file", branch="feature")
        code, report = run_quality("full", repo)
        assert code == 0, json.dumps(report, indent=2)
        entry = pytest_gate(report)
        assert entry["status"] == "skipped", entry
        assert "no tests in repository" in entry["detail"], entry

    def test_collection_error_is_a_suite_failure(self, tmp_path):
        # Measured pytest 9.1.1 with --continue-on-collection-errors: a
        # module that fails at collection exits 1 (not 2) and prints an
        # "ERROR tests/test_syntax.py" summary line — a broken test file
        # is the author's own doing, so it fails the gate with rule
        # pytest/collection instead of exiting 3 as a tooling error.
        repo = make_repo(tmp_path / "broken")
        commit(
            repo,
            {"tests/test_syntax.py": BROKEN_TEST},
            "broken collection",
            branch="feature",
        )
        code, report = run_quality("full", repo)
        assert code == 1, json.dumps(report, indent=2)
        assert report.get("tooling_error") is None, report["tooling_error"]
        entry = pytest_gate(report)
        assert entry["status"] == "fail", entry
        assert entry["mechanism"] == "absolute"
        rules = {f["rule"] for f in report["findings"] if f["gate"] == "pytest"}
        assert "pytest/collection" in rules, rules
        paths = {f["path"] for f in report["findings"] if f["gate"] == "pytest"}
        assert "tests/test_syntax.py" in paths, paths


class TestFastModeAffected:
    def test_changed_module_selects_importing_test_file(self, tmp_path):
        # src/scratch/app.py changed → tests/test_app.py selected via
        # the import heuristic, and the selected suite runs green.
        repo = make_repo(tmp_path / "affected")
        commit(
            repo,
            {
                "src/scratch/app.py": (
                    "def add(a: int, b: int) -> int:\n"
                    "    return a + b\n"
                    "\n"
                    "\n"
                    "def greet(name: str) -> str:\n"
                    '    return f"hello {name}"\n'
                    "\n"
                    "\n"
                    "def farewell(name: str) -> str:\n"
                    '    return f"bye {name}"\n'
                ),
            },
            "change app module",
            branch="feature",
        )
        code, report = run_quality("fast", repo)
        assert code == 0, json.dumps(report, indent=2)
        entry = pytest_gate(report)
        assert entry["status"] == "pass", entry
        assert entry.get("selected") == ["tests/test_app.py"], entry

    def test_unreferenced_changed_module_skips(self, tmp_path):
        # A changed src module no test file imports → no affected tests.
        repo = make_repo(tmp_path / "orphan")
        commit(
            repo,
            {
                "src/scratch/orphan.py": "def unused() -> int:\n    return 1\n",
            },
            "add unreferenced module",
            branch="feature",
        )
        code, report = run_quality("fast", repo)
        assert code == 0, json.dumps(report, indent=2)
        entry = pytest_gate(report)
        assert entry["status"] == "skipped", entry
        assert entry["detail"] == "no affected tests", entry

    def test_changed_test_file_is_always_selected(self, tmp_path):
        repo = make_repo(tmp_path / "testchange")
        commit(
            repo,
            {
                "tests/test_extra.py": (
                    "from scratch.app import greet\n"
                    "\n"
                    "\n"
                    "def test_greet_extra():\n"
                    '    assert greet("x") == "hello x"\n'
                ),
            },
            "add a test file",
            branch="feature",
        )
        code, report = run_quality("fast", repo)
        assert code == 0, json.dumps(report, indent=2)
        entry = pytest_gate(report)
        assert entry["status"] == "pass", entry
        assert "tests/test_extra.py" in entry.get("selected", []), entry


class TestFastModeOff:
    def test_fast_pytest_off_skips_with_probe_decision(self, tmp_path):
        # The narrowed config must exist at BASE already: editing
        # .quality/config.toml in the PR itself trips the integrity
        # gate (protected path), so the patch is amended into the
        # baseline commit instead.
        repo = make_repo(tmp_path / "off")
        config = repo / ".quality" / "config.toml"
        config.write_text(
            config.read_text(encoding="utf-8").replace('pytest = "affected"', 'pytest = "off"'),
            encoding="utf-8",
        )
        run_git("add", "-A", cwd=repo)
        run_git("commit", "--amend", "-m", "clean scratch baseline", cwd=repo)
        commit(
            repo,
            {
                "src/scratch/extra.py": "def extra() -> int:\n    return 42\n",
            },
            "unrelated feature change",
            branch="feature",
        )
        code, report = run_quality("fast", repo)
        assert code == 0, json.dumps(report, indent=2)
        entry = pytest_gate(report)
        assert entry["status"] == "skipped", entry
        assert "off" in entry["detail"], entry
        assert "probe" in entry["detail"], entry


class TestParseSummary:
    """Against the exact short-summary shapes measured on pytest 9.1.1."""

    def test_failed_line_with_reason(self):
        findings = parse_summary(
            "=========================== short test summary info ===========================\n"
            "FAILED tests/test_app.py::test_fail - assert 5 == 6\n"
            "1 failed in 0.01s\n"
        )
        assert len(findings) == 1
        f = findings[0]
        assert (f.rule, f.path, f.symbol, f.message) == (
            "pytest/failure",
            "tests/test_app.py",
            "test_fail",
            "assert 5 == 6",
        )
        assert f.line == 0

    def test_parametrized_and_class_nodeids(self):
        findings = parse_summary(
            "FAILED tests/test_app.py::test_add_params[2-3-6] - assert 5 == 6\n"
            "FAILED tests/test_app.py::TestKlass::test_method - assert 0 == 1\n"
        )
        assert [f.symbol for f in findings] == [
            "test_add_params[2-3-6]",
            "TestKlass::test_method",
        ]
        assert all(f.rule == "pytest/failure" for f in findings)
        assert all(f.path == "tests/test_app.py" for f in findings)

    def test_collection_error_line(self):
        findings = parse_summary("ERROR tests/test_syntax.py\n")
        assert len(findings) == 1
        assert findings[0].rule == "pytest/collection"
        assert findings[0].path == "tests/test_syntax.py"
        assert findings[0].symbol is None

    def test_setup_error_line_is_test_error(self):
        findings = parse_summary("ERROR tests/test_app.py::test_needs_db\n")
        assert findings[0].rule == "pytest/error"
        assert findings[0].symbol == "test_needs_db"

    def test_ansi_escapes_are_stripped(self):
        # pytest 9 colorises the summary even when piped; --color=no is
        # the primary defence, the strip is the second.
        findings = parse_summary("FAILED \x1b[1mtests/test_app.py::test_fail\x1b[0m - boom\n")
        assert findings[0].path == "tests/test_app.py"

    def test_summary_header_and_noise_are_ignored(self):
        assert parse_summary("no tests ran in 0.00s\n") == []
        assert parse_summary("") == []


class TestSelectionHeuristic:
    def test_module_names_derived_from_src_paths(self):
        diff = DiffModel(
            changed_files=frozenset(
                {
                    "src/scratch/app.py",
                    "src/scratch/sub/__init__.py",
                    "src/scratch/__init__.py",
                    "tests/test_app.py",
                    "README.md",
                }
            )
        )
        assert changed_src_modules(diff) == ["scratch", "scratch.app", "scratch.sub"]

    def test_discovery_skips_hidden_and_quality_dirs(self, tmp_path):
        repo = make_repo(tmp_path / "discover")
        (repo / ".quality" / "test_hidden.py").write_text("", encoding="utf-8")
        (repo / ".hidden").mkdir()
        (repo / ".hidden" / "test_x.py").write_text("", encoding="utf-8")
        assert discover_test_files(repo) == ["tests/test_app.py"]

    def test_affected_selection_by_import_reference(self, tmp_path):
        repo = make_repo(tmp_path / "select")
        diff = DiffModel(changed_files=frozenset({"src/scratch/app.py"}))
        assert affected_test_files(repo, diff) == ["tests/test_app.py"]

    def test_unreferenced_module_not_affected(self, tmp_path):
        repo = make_repo(tmp_path / "select2")
        diff = DiffModel(changed_files=frozenset({"src/scratch/orphan.py"}))
        assert affected_test_files(repo, diff) == []

    def test_changed_test_file_always_affected(self, tmp_path):
        repo = make_repo(tmp_path / "select3")
        (repo / "tests" / "test_new.py").write_text(
            "def test_new():\n    assert True\n", encoding="utf-8"
        )
        diff = DiffModel(changed_files=frozenset({"tests/test_new.py"}))
        assert affected_test_files(repo, diff) == ["tests/test_new.py"]

    def test_package_change_matches_submodule_imports(self, tmp_path):
        # src/scratch/__init__.py changed → module "scratch"; a test
        # importing scratch.app references it.
        repo = make_repo(tmp_path / "select4")
        diff = DiffModel(changed_files=frozenset({"src/scratch/__init__.py"}))
        assert affected_test_files(repo, diff) == ["tests/test_app.py"]


# --------------------------------------------------------------------------
# [tests] runner_args passthrough and the optional [tools] pytest-xdist
# project-env pin (CI-speed plan M3, PR R). Landed inert: nothing in this
# repository sets either yet, and the default paths below must stay
# byte-identical to the pre-feature invocation.
# --------------------------------------------------------------------------


def _stub_context(repo: Path, config: QualityConfig | None = None) -> pipeline.GateContext:
    return pipeline.GateContext(
        repo=repo,
        config=config or QualityConfig.load(repo),
        lock=load_toolchain(repo),
        base=BaseRef(source="config", ref="main", sha="0" * 40),
        diff=DiffModel(),
        env=repo / ".nonexistent-analyzer-env",
        cache=repo.parent / "cache",
        report=Report(base=None, command="full"),
        mode="full",
    )


def _python(env: Path) -> str:
    return str(env / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))


def _canonical_args(repo: Path, ctx: pipeline.GateContext) -> list[str]:
    """The adapter's full-mode invocation for this context, spelled out
    — the identity the inert default must preserve exactly."""
    tag = hashlib.sha256(str(repo).encode()).hexdigest()[:12]
    coverage_json = ctx.cache / "coverage" / f"coverage-{tag}.json"
    return [
        "-m",
        "pytest",
        "-c",
        ".quality/pytest.ini",
        "--rootdir",
        str(repo),
        "--color=no",
        "--tb=no",
        "-rfE",
        "-q",
        "-p",
        "no:cacheprovider",
        "--continue-on-collection-errors",
        "--cov=src",
        "--cov-branch",
        "--cov-report=",
        f"--cov-report=json:{coverage_json}",
    ]


def _captured_suite_command(
    repo: Path, monkeypatch: pytest.MonkeyPatch, config: QualityConfig | None = None
) -> list[str]:
    """run_suite's constructed command with the env build and the
    subprocess stubbed: the command shape is the unit under test, the
    suite itself is not."""
    stub_env = repo / "stub-project-env"
    stub_env.mkdir(exist_ok=True)
    monkeypatch.setattr(pytest_adapter, "project_env", lambda *a, **k: stub_env)
    recorded: dict[str, list[str]] = {}

    def fake_run(cmd, **kwargs):
        recorded["cmd"] = list(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="2 passed in 0.01s\n", stderr="")

    monkeypatch.setattr(pytest_adapter.subprocess, "run", fake_run)
    run_suite(_stub_context(repo, config))
    return recorded["cmd"]


class TestRunnerArgsPassthrough:
    """[tests] runner_args must reach the pinned pytest — as pytest
    arguments, after ``-m pytest``, ahead of the canonical flags."""

    def test_default_command_is_exactly_todays(self, tmp_path, monkeypatch):
        # No runner_args (and no xdist pin in the scratch lock): the
        # constructed command is byte-identical to the pre-feature one —
        # the inertness contract of CI-speed plan M3 PR R.
        repo = make_repo(tmp_path / "default")
        cmd = _captured_suite_command(repo, monkeypatch)
        assert cmd == [
            _python(repo / "stub-project-env"),
            *_canonical_args(repo, _stub_context(repo)),
        ]

    def test_runner_args_follow_m_pytest_and_precede_canonical_flags(self, tmp_path, monkeypatch):
        # runner_args are pytest arguments (xdist's "-n auto" is the
        # motivating case): ahead of "-m pytest" they would land on the
        # interpreter and abort with a usage error, not run the suite.
        repo = make_repo(tmp_path / "args")
        config = dataclasses.replace(
            QualityConfig.load(repo),
            tests_runner_args=("-n", "auto", "--dist", "loadgroup"),
        )
        cmd = _captured_suite_command(repo, monkeypatch, config)
        assert cmd[0] == _python(repo / "stub-project-env")
        assert cmd[1:3] == ["-m", "pytest"]
        assert cmd[3:7] == ["-n", "auto", "--dist", "loadgroup"]
        # Today's canonical flags follow, untouched.
        assert cmd[7:] == _canonical_args(repo, _stub_context(repo, config))[2:]

    def test_runner_args_parsed_from_config_toml(self, tmp_path):
        # The [tests] table in .quality/config.toml is the source of the
        # tuple; the passthrough above is only as good as this parse.
        repo = make_repo(tmp_path / "parsed")
        config = repo / ".quality" / "config.toml"
        config.write_text(
            config.read_text(encoding="utf-8").replace(
                "[tests]\n", '[tests]\nrunner_args = ["-n", "auto"]\n', 1
            ),
            encoding="utf-8",
        )
        assert QualityConfig.load(repo).tests_runner_args == ("-n", "auto")


class TestProjectEnvPins:
    """The optional [tools] pytest-xdist pin → project env content."""

    def test_absent_pin_is_todays_pins_exactly(self, tmp_path):
        repo = make_repo(tmp_path / "nopin")
        assert project_env_pins(load_toolchain(repo)) == (
            "pytest==9.1.1",
            "pytest-cov==7.0.0",
        )

    def test_present_pin_installs_xdist_alongside(self, tmp_path):
        repo = make_repo(tmp_path / "pin")
        lockfile = repo / ".quality" / "toolchain.lock"
        # Appended after the last [tools] entry (pyscn), so the line
        # stays inside the table.
        lockfile.write_text(
            lockfile.read_text(encoding="utf-8") + 'pytest-xdist = "3.8.0"\n',
            encoding="utf-8",
        )
        assert project_env_pins(load_toolchain(repo)) == (
            "pytest==9.1.1",
            "pytest-cov==7.0.0",
            "pytest-xdist==3.8.0",
        )

    def test_pinned_xdist_lands_in_the_built_project_env(self, tmp_path):
        # Beyond the pin list: a real build must complete and contain
        # the plugin. pytest-xdist ships no console script, so this also
        # proves the env-completeness check does not demand
        # bin/pytest-xdist (plugin-only tools are exempt).
        repo = make_repo(tmp_path / "built")
        lockfile = repo / ".quality" / "toolchain.lock"
        lockfile.write_text(
            lockfile.read_text(encoding="utf-8") + 'pytest-xdist = "3.8.0"\n',
            encoding="utf-8",
        )
        env = project_env(repo, load_toolchain(repo))
        proc = subprocess.run(
            [
                _python(env),
                "-c",
                "from importlib.metadata import version; print(version('pytest-xdist'))",
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "3.8.0"
