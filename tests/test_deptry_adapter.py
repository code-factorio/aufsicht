"""deptry adapter self-tests (v5.1 §3 "Dependency hygiene | deptry |
per-rule ratchet (DEP001–DEP005)", §4.3, §4.4).

Each case: a scratch repo with a real merge base, a real violation
applied, the real CLI invoked (distribution spec §8). The DEP code
shapes asserted here were measured against pinned deptry 0.25.1:

  * ``import flask`` with ``dependencies = []`` → DEP001 (flask is not
    in the analyzer environment's site-packages; a module that IS
    installed there but undeclared would be DEP003 "transitive").
  * a declared-but-unimported dependency → DEP002 with
    ``location.line = null`` (pyproject-level finding).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aufsicht.adapters.deptry import parse_findings
from aufsicht.errors import ToolingError
from tests.conftest import run_cli
from tests.fixtures.scratch import commit, make_repo

# app.py variants. All ruff-format clean; flask is referenced only in
# an annotation so the import is used (F401) without executing flask.
APP_IMPORTS_FLASK = '''\
"""A deliberately small, clean application module."""
import flask


def add(a: int, b: int) -> int:
    return a + b


def greet(name: str) -> str:
    return f"hello {name}"


def fetch(url: str) -> flask.Response:
    raise NotImplementedError
'''

APP_NO_FLASK = '''\
"""A deliberately small, clean application module."""


def add(a: int, b: int) -> int:
    return a + b


def greet(name: str) -> str:
    return f"hello {name}"


def farewell(name: str) -> str:
    return f"bye {name}"
'''

APP_FLASK_PLUS_FAREWELL = '''\
"""A deliberately small, clean application module."""
import flask


def add(a: int, b: int) -> int:
    return a + b


def greet(name: str) -> str:
    return f"hello {name}"


def fetch(url: str) -> flask.Response:
    raise NotImplementedError


def farewell(name: str) -> str:
    return f"bye {name}"
'''

# Two declared dependencies, one imported (flask), one unused (boto3):
# a BASE that already carries one DEP002, so the ratchet compares
# 1 → 1 (pass) and 1 → 2 (regress) below.
PYPROJECT_TWO_DEPS = """\
[project]
name = "scratch"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["flask", "boto3"]
"""

PYPROJECT_OPTIONAL_DEPS = """\
[project]
name = "scratch"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = []

[project.optional-dependencies]
web = ["flask"]
"""


def run_full(repo: Path) -> tuple[int, dict]:
    proc = run_cli("full", cwd=repo)
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            f"quality-full produced no JSON report.\nstdout: {proc.stdout[:500]}\n"
            f"stderr: {proc.stderr[:2000]}"
        )
    return proc.returncode, report


def _repo_with_declared_deps(path: Path) -> Path:
    """A scratch repo whose merge base declares flask+boto3, imports
    flask, and never imports boto3: exactly one DEP002 at BASE."""
    repo = make_repo(path)
    commit(
        repo,
        {"pyproject.toml": PYPROJECT_TWO_DEPS, "src/scratch/app.py": APP_IMPORTS_FLASK},
        "declare flask+boto3, use flask only",
    )
    return repo


class TestDep001MissingDependency:
    def test_import_without_declaration_fails_per_rule_ratchet(self, tmp_path):
        # §4.3: DEP001 base 0 → head 1 regresses the per-rule ratchet.
        # The pyproject is unchanged between BASE and HEAD, so no §4.5
        # exemption applies and the ratchet must actually compare.
        repo = make_repo(tmp_path / "dep001")
        commit(
            repo,
            {"src/scratch/app.py": APP_IMPORTS_FLASK},
            "import flask without declaring it",
            branch="feature",
        )
        code, report = run_full(repo)
        assert code == 1, json.dumps(report, indent=2)
        assert report["status"] == "fail"
        entry = report["gates"]["deptry"]
        assert entry["status"] == "fail", entry
        assert "per-rule-ratchet" in entry["mechanism"], entry
        # The ratchet detail: the regressed code with both integers.
        assert entry["ratchet"]["regressed_rules"] == [
            {"rule": "DEP001", "base": 0, "head": 1}
        ], entry["ratchet"]
        rules = {f["rule"] for f in report["findings"] if f["gate"] == "deptry"}
        assert "deptry/DEP001" in rules, rules
        message = next(
            f["detail"] for f in report["findings"] if f["gate"] == "deptry"
        )
        assert "base 0" in message and "head 1" in message, message


class TestDep002UnusedDependency:
    def test_steady_state_unused_dep_passes(self, tmp_path):
        # The ratchet's honest pass: the unused boto3 was already unused
        # at BASE (1 → 1). Only the deptry gate is asserted — other
        # gates' verdicts on the same scratch are theirs, not deptry's.
        repo = _repo_with_declared_deps(tmp_path / "dep002-steady")
        commit(
            repo,
            {"src/scratch/app.py": APP_FLASK_PLUS_FAREWELL},
            "unrelated change",
            branch="feature",
        )
        code, report = run_full(repo)
        entry = report["gates"]["deptry"]
        assert entry["status"] == "pass", entry
        assert entry["ratchet"]["regressed_rules"] == [], entry["ratchet"]
        assert entry["ratchet"]["totals"] == {"base": 1, "head": 1}, entry["ratchet"]

    def test_removing_the_last_import_regresses(self, tmp_path):
        # Removing the import makes flask join boto3 in the unused set:
        # DEP002 1 → 2 via a code-only change (the declared dependency
        # set — hence the project environment — is identical on both
        # sides, so no §4.4 exemption masks the regression).
        repo = _repo_with_declared_deps(tmp_path / "dep002-regress")
        commit(
            repo,
            {"src/scratch/app.py": APP_NO_FLASK},
            "drop the flask import",
            branch="feature",
        )
        code, report = run_full(repo)
        assert code == 1, json.dumps(report, indent=2)
        entry = report["gates"]["deptry"]
        assert entry["status"] == "fail", entry
        assert "per-rule-ratchet" in entry["mechanism"], entry
        assert entry["ratchet"]["regressed_rules"] == [
            {"rule": "DEP002", "base": 1, "head": 2}
        ], entry["ratchet"]
        findings = [f for f in report["findings"] if f["gate"] == "deptry"]
        assert findings, report["findings"]
        assert findings[0]["rule"] == "deptry/DEP002"
        assert "flask" in findings[0]["detail"], findings[0]


class TestEnvironmentSensitivity:
    def test_project_lockfile_change_exempts_deptry(self, tmp_path):
        # §4.4: when the project dependency specification differs
        # between BASE and HEAD the comparison is unsound; §4.5 exempts
        # the environment-sensitive ratchets and §15 requires the
        # exemption to be visible. It is load-bearing here: the unused
        # optional dependency would otherwise be a fresh DEP002.
        repo = make_repo(tmp_path / "lockfile-change")
        commit(
            repo,
            {"pyproject.toml": PYPROJECT_OPTIONAL_DEPS},
            "add optional-dependencies",
            branch="feature",
        )
        code, report = run_full(repo)
        assert code == 0, json.dumps(report, indent=2)
        assert report["dependency_environment_changed"] is True
        assert "deptry" in report["exempt_tools"], report["exempt_tools"]
        entry = report["gates"]["deptry"]
        assert entry["status"] == "exempt", entry
        assert entry["ratchet"] == "exempt", entry
        assert entry["ratchet_reason"], entry
        assert not [f for f in report["findings"] if f["gate"] == "deptry"]

    def test_deptry_gate_is_full_mode_only(self, tmp_path):
        # The §3 table puts dependency hygiene on quality-full only.
        repo = make_repo(tmp_path / "fast-mode")
        proc = run_cli("fast", cwd=repo)
        report = json.loads(proc.stdout)
        assert "deptry" not in report["gates"], report["gates"].keys()


class TestParser:
    """Unit tests against the measured deptry 0.25.1 JSON shape."""

    def test_dep001_entry_shape(self):
        raw = json.dumps([
            {
                "error": {
                    "code": "DEP001",
                    "message": "'flask' imported but missing from the dependency definitions",
                },
                "module": "flask",
                "location": {"file": "src/scratch/app.py", "line": 2, "column": 8},
            }
        ])
        findings = parse_findings(raw)
        assert len(findings) == 1
        f = findings[0]
        assert (f.rule, f.path, f.line, f.end_line, f.symbol) == (
            "DEP001", "src/scratch/app.py", 2, 2, "flask"
        )
        assert "missing from the dependency definitions" in f.message

    def test_dep002_null_location_lines(self):
        # Measured: pyproject-level findings carry line/column null.
        raw = json.dumps([
            {
                "error": {
                    "code": "DEP002",
                    "message": "'boto3' defined as a dependency but not used in the codebase",
                },
                "module": "boto3",
                "location": {"file": "pyproject.toml", "line": None, "column": None},
            }
        ])
        findings = parse_findings(raw)
        assert findings[0].line == 0 and findings[0].path == "pyproject.toml"

    def test_empty_and_unparseable_output(self):
        assert parse_findings("[]") == []
        assert parse_findings("") == []
        with pytest.raises(ToolingError):
            parse_findings("deptry: not json at all")
        with pytest.raises(ToolingError):
            parse_findings('{"error": {}}')  # an object, not a list
