"""Pyright adapter self-tests: the v5.1 §18 pyright rows and the §12
strict-list rule, executed against real scratch repositories with the
real CLI (distribution spec §8).

The three §18 rows:

    type error in a changed file        → Pyright, diff-scoped
                                          (quality-fast) and per-rule
                                          ratchet (quality-full)
    type error caused in another file   → Pyright, per-rule ratchet;
                                          the fast gate correctly does
                                          NOT see it (v5.1 §4.2 known
                                          limit)
    pyright syntax error, rule = null   → per-rule ratchet, "<no-rule>"
                                          bucket

plus §12: a new top-level module under src/ must land in the strict
list, and touching pyrightconfig.json to add it is a protected-path
change the integrity gate fires on.

Empirical basis (pinned pyright 1.1.411, measured before writing the
parser): ``--outputjson`` emits ``generalDiagnostics`` on stdout with
0-based ``range.start.line`` and ``rule`` present for type errors
(reportReturnType, reportCallIssue, reportArgumentType, ...) but null
for syntax errors; exit 0 clean / 1 with diagnostics.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aufsicht.adapters.pyright import (
    _in_strict,
    missing_strict_modules,
    parse_diagnostics,
)
from aufsicht.errors import ToolingError
from tests.conftest import run_cli, run_git
from tests.fixtures.scratch import commit, make_repo
from tests.test_selftests import run_full

# Real shape captured from the pinned pyright 1.1.411 (probe, not
# guessed): a typed error with a rule and a syntax error without one.
REAL_OUTPUT = """{
  "version": "1.1.411",
  "time": "1786913118792",
  "generalDiagnostics": [
    {
      "file": "/tmp/repo/src/scratch/app.py",
      "severity": "error",
      "message": "Type \\"int\\" is not assignable to return type \\"str\\"\\n  ...",
      "range": {"start": {"line": 1, "character": 11}, "end": {"line": 1, "character": 12}},
      "rule": "reportReturnType"
    },
    {
      "file": "/tmp/repo/src/scratch/broken.py",
      "severity": "error",
      "message": "\\"(\\" was not closed",
      "range": {"start": {"line": 0, "character": 12}, "end": {"line": 0, "character": 13}}
    }
  ],
  "summary": {"filesAnalyzed": 2, "errorCount": 2, "warningCount": 0,
              "informationCount": 0, "timeInSec": 0.3}
}
"""

TYPE_ERROR_APP = (
    "def add(a: int, b: int) -> int:\n"
    '    return "oops"\n'
    "\n"
    "\n"
    "def greet(name: str) -> str:\n"
    '    return f"hello {name}"\n'
)

THREE_ARG_APP = (
    "def add(a: int, b: int, c: int) -> int:\n"
    "    return a + b + c\n"
    "\n"
    "\n"
    "def greet(name: str) -> str:\n"
    '    return f"hello {name}"\n'
)

SYNTAX_ERROR = "def broken(:\n    pass\n"

NEW_MODULE = (
    '"""A brand-new top-level module, absent from the strict list."""\n'
    "\n"
    "\n"
    "def helper(x: int) -> int:\n"
    "    return x * 2\n"
)


def run_fast(repo: Path) -> tuple[int, dict]:
    proc = run_cli("fast", cwd=repo)
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            f"quality-fast produced no JSON report.\nstdout: {proc.stdout[:500]}\n"
            f"stderr: {proc.stderr[:2000]}"
        )
    return proc.returncode, report


def main_sha(repo: Path) -> str:
    return run_git("rev-parse", "main", cwd=repo).stdout.strip()


# Gates that run AFTER pyright / pyright-fast in GATE_ORDER and belong
# to parallel adapter workstreams. They are disabled in these scratch
# repos so a crash or failure in one of them cannot abort the run (or
# its exit code) once the pyright gates have already executed — the §18
# pyright rows under test stay deterministic. Everything BEFORE pyright
# (integrity, ruff, ruff-s, suppressions, complexity) stays live, and
# integrity is load-bearing for the §12 protected-path assertions.
POST_PYRIGHT_GATES = (
    "pytest",
    "semgrep",
    "xenon",
    "cycles",
    "deadcode",
    "deptry",
    "pip-audit",
)


def make_pyright_repo(path: Path, *extra_disable: str) -> Path:
    """A scratch repo whose baseline disables the post-pyright gates.

    The disable list is part of the baseline commit (not a working-tree
    edit), so the integrity gate is not tripped by the harness itself.
    """
    repo = make_repo(path)
    gates = ", ".join(f'"{g}"' for g in sorted({*POST_PYRIGHT_GATES, *extra_disable}))
    config = (repo / ".quality" / "config.toml").read_text(encoding="utf-8")
    config += f"\n[gates]\ndisable = [{gates}]\n"
    (repo / ".quality" / "config.toml").write_text(config, encoding="utf-8")
    run_git("add", "-A", cwd=repo)
    run_git("commit", "-q", "--amend", "-m", "clean scratch baseline", cwd=repo)
    return repo


class TestParsing:
    def test_null_rule_is_bucketed_and_lines_are_1_based(self):
        raw, findings = parse_diagnostics(REAL_OUTPUT)
        assert [d.get("rule") for d in raw] == ["reportReturnType", None]
        assert findings[0].rule == "reportReturnType"
        assert findings[0].line == 2  # pyright range lines are 0-based
        assert findings[1].rule == "<no-rule>"  # v5.1 §4.3 reserved bucket
        assert findings[1].line == 1

    def test_raw_rules_ratchet_through_count_by_rule(self):
        # The null must reach ratchet.count_by_rule intact so the
        # "<no-rule>" bucket is the ratchet's, not the parser's (§4.3).
        from aufsicht.ratchet import count_by_rule

        raw, _ = parse_diagnostics(REAL_OUTPUT)
        assert count_by_rule([d.get("rule") for d in raw]) == {
            "reportReturnType": 1,
            "<no-rule>": 1,
        }

    def test_unparseable_output_is_a_tooling_error(self):
        with pytest.raises(ToolingError):
            parse_diagnostics("pyright: node not found")


class TestStrictListUnit:
    def test_new_module_without_strict_entry_is_missing(self, tmp_path):
        repo = make_repo(tmp_path / "s1")
        commit(repo, {"src/extra/mod.py": NEW_MODULE}, "new module", branch="feature")
        assert missing_strict_modules(repo, main_sha(repo)) == ["extra"]

    def test_module_present_in_strict_list_is_not_missing(self, tmp_path):
        repo = make_repo(tmp_path / "s2")
        commit(
            repo,
            {
                "src/extra/mod.py": NEW_MODULE,
                "pyrightconfig.json": json.dumps(
                    {
                        "typeCheckingMode": "basic",
                        "strict": ["src/extra"],
                        "exclude": [".quality", ".venv"],
                    },
                    indent=2,
                ),
            },
            "new module, listed",
            branch="feature",
        )
        assert missing_strict_modules(repo, main_sha(repo)) == []

    def test_strict_entry_forms(self):
        for entry in (
            "extra",
            "src/extra",
            "src/extra/",
            "src/extra/**",
            "./src/extra",
        ):
            assert _in_strict("extra", [entry]), entry
        for entry in ("src", "src/extras", "extras"):
            assert not _in_strict("extra", [entry]), entry


class TestTypeErrorInChangedFile:
    """§18: type error in a changed file → Pyright, diff-scoped."""

    def test_fast_gate_fails_diff_scoped(self, tmp_path):
        repo = make_pyright_repo(tmp_path / "changed-fast")
        commit(
            repo, {"src/scratch/app.py": TYPE_ERROR_APP}, "type error", branch="feature"
        )
        code, report = run_fast(repo)
        assert code == 1, report.get("tooling_error")
        assert report["status"] == "fail"
        entry = report["gates"]["pyright-fast"]
        assert entry["status"] == "fail", entry
        assert entry["mechanism"] == "diff-scoped"
        mine = [f for f in report["findings"] if f["gate"] == "pyright-fast"]
        assert [(f["rule"], f["path"]) for f in mine] == [
            ("reportReturnType", "src/scratch/app.py")
        ], mine

    def test_full_run_regresses_the_rule_bucket(self, tmp_path):
        repo = make_pyright_repo(tmp_path / "changed-full")
        commit(
            repo, {"src/scratch/app.py": TYPE_ERROR_APP}, "type error", branch="feature"
        )
        code, report = run_full(repo)
        assert code == 1, report.get("tooling_error")
        entry = report["gates"]["pyright"]
        assert entry["status"] == "fail", entry
        assert "per-rule-ratchet" in entry["mechanism"]
        ratchet = entry["ratchet"]
        assert {"rule": "reportReturnType", "base": 0, "head": 1} in ratchet[
            "regressed_rules"
        ]
        # v5.1 §15: regressed rules with both integers, totals separate.
        assert ratchet["totals"]["base"] == 0
        assert ratchet["totals"]["head"] == 1


class TestTypeErrorCausedInAnotherFile:
    """§18: type error caused in another file → Pyright, per-rule ratchet.

    add() grows a third parameter in src/scratch/app.py (the only
    changed file); tests/test_app.py is UNCHANGED and now calls it with
    two arguments. The changed-file fast gate correctly does not see
    it — that is the documented §4.2 known limit, and the reason the
    repo-wide ratchet is the correctness boundary (§12).
    """

    def test_full_ratchet_catches_the_unchanged_consumer(self, tmp_path):
        repo = make_pyright_repo(tmp_path / "cross-file")
        commit(
            repo, {"src/scratch/app.py": THREE_ARG_APP}, "widen add()", branch="feature"
        )
        _code, report = run_full(repo)
        entry = report["gates"]["pyright"]
        assert entry["status"] == "fail", entry
        assert "per-rule-ratchet" in entry["mechanism"]
        regressed = {r["rule"]: r for r in entry["ratchet"]["regressed_rules"]}
        assert "reportCallIssue" in regressed, regressed
        assert regressed["reportCallIssue"]["base"] == 0
        assert regressed["reportCallIssue"]["head"] >= 1

    def test_fast_gate_correctly_does_not_see_it(self, tmp_path):
        repo = make_pyright_repo(tmp_path / "cross-file-fast")
        commit(
            repo, {"src/scratch/app.py": THREE_ARG_APP}, "widen add()", branch="feature"
        )
        _code, report = run_fast(repo)
        # Only the pyright-fast entry is asserted (the post-pyright
        # gates are disabled in this scratch repo): a diff-scoped run
        # over the changed file alone has no error to report.
        entry = report["gates"]["pyright-fast"]
        assert entry["status"] == "pass", entry
        assert entry["mechanism"] == "diff-scoped"


class TestSyntaxErrorNoRule:
    """§18: pyright syntax error, rule = null → per-rule ratchet,
    "<no-rule>" bucket.

    The ruff gate is additionally disabled in this scratch repo's
    config: the tracer adapter's `ruff format --check` exits 2 on an
    unparseable file, which is a tooling error (exit 3) that aborts the
    pipeline before pyright runs. Disabling it here isolates the row
    under test to Pyright's own mechanism.
    """

    def test_no_rule_bucket_regresses(self, tmp_path):
        repo = make_pyright_repo(tmp_path / "syntax", "ruff")
        commit(
            repo,
            {"src/scratch/broken.py": SYNTAX_ERROR},
            "syntax error",
            branch="feature",
        )
        _code, report = run_full(repo)
        assert report["gates"]["ruff"]["status"] == "skipped"
        entry = report["gates"]["pyright"]
        assert entry["status"] == "fail", entry
        regressed = {r["rule"]: r for r in entry["ratchet"]["regressed_rules"]}
        assert "<no-rule>" in regressed, regressed
        assert regressed["<no-rule>"]["base"] == 0
        assert regressed["<no-rule>"]["head"] >= 1


class TestStrictListEndToEnd:
    """§12: new top-level module under src/ must join the strict list;
    editing pyrightconfig.json to add it is a protected-path change."""

    def test_new_module_not_in_strict_list_fails_gate(self, tmp_path):
        repo = make_pyright_repo(tmp_path / "strict-missing")
        commit(repo, {"src/extra/mod.py": NEW_MODULE}, "new module", branch="feature")
        code, report = run_full(repo)
        assert code == 1, report.get("tooling_error")
        entry = report["gates"]["pyright"]
        assert entry["status"] == "fail", entry
        # The strict-list check is absolute, not a ratchet slip.
        assert "absolute" in entry["mechanism"]
        assert "per-rule-ratchet" in entry["mechanism"]
        findings = [f for f in report["findings"] if f["gate"] == "pyright"]
        assert [(f["rule"], f["path"]) for f in findings] == [
            ("pyright/strict-list", "src/extra")
        ], findings
        assert '"strict"' in findings[0]["detail"]  # actionable (v5.1 §15)
        # The module itself is clean: the ratchet did not regress.
        assert entry["ratchet"]["regressed_rules"] == []

    def test_adding_it_to_strict_list_fires_integrity(self, tmp_path):
        repo = make_pyright_repo(tmp_path / "strict-added")
        commit(
            repo,
            {
                "src/extra/mod.py": NEW_MODULE,
                "pyrightconfig.json": json.dumps(
                    {
                        "typeCheckingMode": "basic",
                        "strict": ["src/extra"],
                        "exclude": [".quality", ".venv"],
                    },
                    indent=2,
                ),
            },
            "new module, listed (guardrail change)",
            branch="feature",
        )
        code, report = run_full(repo)
        # pyrightconfig.json is protected (v5.1 §11.2): the integrity
        # gate fires even though the edit is the *correct* one.
        integrity = report["gates"]["integrity"]
        assert integrity["status"] == "fail", integrity
        assert "pyrightconfig.json" in integrity["detail"]
        # ...and the pyright ratchet is exempt for this PR (v5.1 §4.5),
        # visibly, while the strict-list check itself is satisfied.
        entry = report["gates"]["pyright"]
        assert entry["status"] == "exempt", entry
        assert entry["ratchet"] == "exempt"
        assert "pyright" in report["exempt_tools"]
        assert report["dependency_environment_changed"] is False
        assert code == 1


class TestFastProbeDecision:
    """§6: fast.pyright = "off" skips the probe-narrowed gate visibly."""

    def test_off_skips_with_probe_detail(self, tmp_path):
        repo = make_pyright_repo(tmp_path / "fast-off")
        config = (repo / ".quality" / "config.toml").read_text(encoding="utf-8")
        config = config.replace('pyright = "changed-files"', 'pyright = "off"')
        commit(
            repo,
            {".quality/config.toml": config, "src/scratch/app.py": TYPE_ERROR_APP},
            "narrow pyright away",
            branch="feature",
        )
        _code, report = run_fast(repo)
        entry = report["gates"]["pyright-fast"]
        assert entry["status"] == "skipped", entry
        assert "probe" in entry["detail"]
        assert "correctness boundary" in entry["detail"]
        # The integrity gate fires on the .quality edit; the exit code
        # is its, not pyright-fast's.


class TestCleanCopy:
    def test_clean_scratch_pyright_gate_passes(self, tmp_path):
        repo = make_pyright_repo(tmp_path / "clean")
        _code, report = run_full(repo)
        entry = report["gates"]["pyright"]
        assert entry["status"] == "pass", entry
        assert entry["mechanism"] == "per-rule-ratchet"
        assert entry["ratchet"]["regressed_rules"] == []
        assert (
            entry["ratchet"]["totals"]["base"]
            == entry["ratchet"]["totals"]["head"]
            == 0
        )
