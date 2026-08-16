"""Semgrep adapter self-tests (v5.1 §18 rows 5-7, §9).

Each case: a generated scratch repository with a real merge base, a
real violation committed, the real CLI invoked (`python -m aufsicht
full`), asserting (1) the semgrep gate fails, (2) via the diff-scoped
mechanism, (3) with exit code 1 — plus the scoping negatives (v5.1
§4.2: a legacy skip elsewhere in a changed file must not fire) and the
clean pass.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import run_cli
from tests.fixtures.scratch import commit, make_repo

# --- fixtures (ruff-check/format clean against the pinned ruff,
# measured — the full run includes the ruff gates) -----------------------

SKIP_TEST = """\
import pytest

from scratch.app import add


@pytest.mark.skip(reason="flaky under load")
def test_addition():
    assert add(2, 3) == 5
"""

NO_VERIFY_TEST = """\
from scratch.app import add, greet


def test_report_is_assembled():
    numbers = [1, 2, 3]
    total = sum(numbers)
    greeting = greet("ada")
    add(total, len(greeting))
"""

MOCK_TEST = """\
from unittest.mock import MagicMock


def test_compute_is_mocked():
    m = MagicMock()
    result = m.compute()
    assert result
"""

REAL_TEST = """\
from scratch.app import add


def test_add_with_negatives():
    assert add(-2, 2) == 0
"""

# `assert x == x` is trivially true; `assert x == y` is a real
# comparison — the rule must distinguish them (measured: semgrep
# metavariable equality does, no adapter post-filter needed).
TRIVIAL_OPERANDS_TEST = """\
from scratch.app import add


def test_identical_operands():
    x = add(1, 2)
    y = add(2, 3)
    assert x == x
    assert x == y
"""

# Base test file for the scoping negatives: a legacy skip (line 6) the
# feature commits never touch.
LEGACY_BASE_TEST = """\
import pytest

from scratch.app import add, greet


@pytest.mark.skip(reason="legacy: needs hardware")
def test_legacy_addition():
    assert add(1, 1) == 2


def test_add():
    assert add(2, 3) == 5


def test_greet():
    assert greet("ada") == "hello ada"
"""

# Same file with a test appended: added lines are 17-20 only, so the
# unchanged skip on line 6 is out of added-line scope (v5.1 §4.2).
LEGACY_APPENDED_TEST = LEGACY_BASE_TEST + """\


def test_greet_uppercase():
    assert greet("Ada") == "hello Ada"
"""


def run_full(repo: Path) -> tuple[int, dict]:
    return _run_gate("full", repo)


def run_fast(repo: Path) -> tuple[int, dict]:
    return _run_gate("fast", repo)


def _run_gate(command: str, repo: Path) -> tuple[int, dict]:
    proc = run_cli(command, cwd=repo)
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            f"{command} produced no JSON report.\nstdout: {proc.stdout[:500]}\n"
            f"stderr: {proc.stderr[:2000]}"
        ) from None
    return proc.returncode, report


def assert_gate_failure(
    report: dict, gate: str, mechanism: str, exit_code: int, returncode: int
) -> dict:
    assert returncode == exit_code, (
        f"expected exit {exit_code}, got {returncode}; report: "
        f"{json.dumps(report.get('tooling_error'), indent=2)}"
    )
    assert report["status"] == "fail"
    entry = report["gates"][gate]
    assert entry["status"] == "fail", f"gate {gate} did not fail: {entry}"
    if mechanism is not None:
        assert mechanism in entry["mechanism"], (
            f"gate {gate} mechanism {entry['mechanism']!r} does not declare {mechanism!r}"
        )
    return entry


def semgrep_rules(report: dict) -> set[str]:
    return {f["rule"] for f in report["findings"] if f["gate"] == "semgrep"}


# The adapter is imported lazily: parallel work on this tree can be
# rebuilding the editable install while tests collect, and a collection
# error would hide the actual results (the CLI subprocesses below build
# their own import context).
@pytest.fixture
def adapter():
    from aufsicht.adapters import semgrep

    return semgrep


class TestSemgrepDiffScoped:
    def test_skip_decorator_added_fails(self, tmp_path):
        # §18: "@pytest.mark.skip added → Semgrep, diff-scoped"
        repo = make_repo(tmp_path / "skip")
        commit(repo, {"tests/test_skipped.py": SKIP_TEST}, "disable a test", branch="feature")
        code, report = run_full(repo)
        entry = assert_gate_failure(report, "semgrep", "diff-scoped", 1, code)
        assert entry["detail"], "failure must carry an actionable detail (v5.1 §15)"
        assert "pytest-test-disabled" in semgrep_rules(report), semgrep_rules(report)

    def test_no_verification_act_fails(self, tmp_path):
        # §18: "test with no verification act → Semgrep, diff-scoped".
        # The body only assembles values; no assert, no act-set call.
        repo = make_repo(tmp_path / "noverify")
        commit(
            repo, {"tests/test_no_verify.py": NO_VERIFY_TEST},
            "test verifies nothing", branch="feature",
        )
        code, report = run_full(repo)
        assert_gate_failure(report, "semgrep", "diff-scoped", 1, code)
        rules = semgrep_rules(report)
        assert rules == {"test/no-verification"}, rules
        finding = next(f for f in report["findings"] if f["gate"] == "semgrep")
        assert finding["symbol"] == "test_report_is_assembled", finding
        assert finding["path"] == "tests/test_no_verify.py", finding

    def test_assert_on_magic_mock_fails(self, tmp_path):
        # §18: "test asserting on a MagicMock → Semgrep, diff-scoped"
        # (MagicMock is unconditionally truthy, v5.1 §9.2). The taint
        # rule must bind `m = MagicMock()` and propagate through
        # `result = m.compute()` to `assert result`.
        repo = make_repo(tmp_path / "mock")
        commit(repo, {"tests/test_mocked.py": MOCK_TEST}, "assert on a mock", branch="feature")
        code, report = run_full(repo)
        assert_gate_failure(report, "semgrep", "diff-scoped", 1, code)
        rules = semgrep_rules(report)
        assert "trivial-assert-mock" in rules, rules
        # `assert result` IS an assert, so the no-verification gate must
        # stay silent — the two checks are separate (v5.1 §9.1 vs §9.2).
        assert "test/no-verification" not in rules, rules

    def test_trivial_assert_requires_identical_operands(self, tmp_path):
        # §9.2: `assert x == x` fires, `assert x == y` must NOT —
        # exactly one finding, on the identical-operand line.
        repo = make_repo(tmp_path / "operands")
        commit(
            repo, {"tests/test_operands.py": TRIVIAL_OPERANDS_TEST},
            "trivial and real comparison", branch="feature",
        )
        code, report = run_full(repo)
        assert_gate_failure(report, "semgrep", "diff-scoped", 1, code)
        findings = [f for f in report["findings"] if f["gate"] == "semgrep"]
        assert len(findings) == 1, findings
        assert findings[0]["rule"] == "trivial-assert-constant"
        assert findings[0]["line"] == 7, findings  # the `assert x == x` line

    def test_skip_decorator_fails_in_fast_mode_too(self, tmp_path):
        # The gate runs in both loops (v5.1 §3); fast stays diff-scoped.
        repo = make_repo(tmp_path / "skip-fast")
        commit(repo, {"tests/test_skipped.py": SKIP_TEST}, "disable a test", branch="feature")
        code, report = run_fast(repo)
        assert_gate_failure(report, "semgrep", "diff-scoped", 1, code)
        assert "pytest-test-disabled" in semgrep_rules(report), semgrep_rules(report)

    def test_legacy_skip_in_unchanged_file_not_flagged(self, tmp_path):
        # v5.1 §4.2: running over changed files must not report an
        # untouched legacy skip elsewhere in the repo.
        repo = make_repo(tmp_path / "legacy-file", test=LEGACY_BASE_TEST)
        commit(repo, {
            "src/scratch/app.py": (
                "def add(a: int, b: int) -> int:\n"
                "    return a + b\n"
                "\n"
                "\n"
                "def greet(name: str) -> str:\n"
                "    return f\"hello {name}\"\n"
                "\n"
                "\n"
                "def farewell(name: str) -> str:\n"
                "    return f\"bye {name}\"\n"
            ),
        }, "unrelated change", branch="feature")
        code, report = run_full(repo)
        assert code == 0, json.dumps(report, indent=2)
        assert report["gates"]["semgrep"]["status"] == "pass", report["gates"]["semgrep"]

    def test_skip_on_unchanged_line_of_changed_file_not_flagged(self, tmp_path):
        # v5.1 §4.2: skip/xfail is ADDED-LINE scoped. The file changed
        # (a test appended), but the skip sits on unchanged lines.
        repo = make_repo(tmp_path / "legacy-line", test=LEGACY_BASE_TEST)
        commit(
            repo, {"tests/test_app.py": LEGACY_APPENDED_TEST},
            "append an unrelated test", branch="feature",
        )
        code, report = run_full(repo)
        assert code == 0, json.dumps(report, indent=2)
        assert report["gates"]["semgrep"]["status"] == "pass", report["gates"]["semgrep"]

    def test_real_assert_passes(self, tmp_path):
        # The negative: a genuine verification act in a changed test.
        repo = make_repo(tmp_path / "real")
        commit(repo, {"tests/test_real.py": REAL_TEST}, "add a real test", branch="feature")
        code, report = run_full(repo)
        assert code == 0, json.dumps(report, indent=2)
        assert report["gates"]["semgrep"]["status"] == "pass", report["gates"]["semgrep"]
        assert report["gates"]["semgrep"]["mechanism"] == "diff-scoped"


class TestSemgrepParsing:
    """Unit cases for the normalisation the gate is built on (the tool
    itself is exercised by the scratch runs above)."""

    def test_parse_findings_normalises_check_id_and_span(self, adapter, tmp_path):
        raw = json.dumps({
            "results": [
                {
                    "check_id": "quality.semgrep.pytest-test-disabled",
                    "path": "tests/test_x.py",
                    "start": {"line": 5},
                    "end": {"line": 5},
                    "extra": {"severity": "ERROR", "message": "test disabled"},
                },
                {
                    "check_id": "quality.semgrep.verification-act-assert",
                    "path": str(tmp_path / "tests" / "test_x.py"),
                    "start": {"line": 6},
                    "end": {"line": 9},
                    "extra": {"severity": "INFO", "message": "act"},
                },
            ]
        })
        findings = adapter.parse_findings(raw, tmp_path)
        assert [(f.rule, f.line, f.end_line, f.severity) for f in findings] == [
            ("pytest-test-disabled", 5, 5, "error"),
            ("verification-act-assert", 6, 9, "info"),
        ]
        # absolute paths from the tool are made repo-relative
        assert findings[1].path == "tests/test_x.py"

    def test_no_verification_changed_function_without_act(self, adapter, tmp_path):
        # The §4.2 row "verification absence | changed test functions":
        # span intersecting added lines + no overlapping act.
        DiffModel, Finding = adapter.DiffModel, adapter.Finding

        target = tmp_path / "tests" / "test_x.py"
        target.parent.mkdir(parents=True)
        target.write_text(
            "def test_widget():\n    pass\n\n\ndef other():\n    pass\n",
            encoding="utf-8",
        )
        diff = DiffModel(
            changed_files=frozenset({"tests/test_x.py"}),
            added_lines={"tests/test_x.py": [(1, 2)]},
        )
        fn = Finding(
            path="tests/test_x.py", line=1, end_line=2,
            rule="pytest-test-function", message="", severity="info",
        )
        out = adapter.no_verification_findings([fn], diff, tmp_path)
        assert len(out) == 1
        assert out[0].rule == "test/no-verification"
        assert out[0].symbol == "test_widget"
        assert (out[0].line, out[0].end_line) == (1, 2)

    def test_overlapping_act_or_unchanged_function_is_not_a_violation(self, adapter, tmp_path):
        DiffModel, Finding = adapter.DiffModel, adapter.Finding

        target = tmp_path / "tests" / "test_x.py"
        target.parent.mkdir(parents=True)
        target.write_text(
            "def test_covered():\n    assert 1 + 1 == 2\n"
            "\n\ndef test_untouched():\n    pass\n",
            encoding="utf-8",
        )
        diff = DiffModel(
            changed_files=frozenset({"tests/test_x.py"}),
            added_lines={"tests/test_x.py": [(1, 2)]},
        )
        covered = Finding(
            path="tests/test_x.py", line=1, end_line=2,
            rule="pytest-test-function", message="", severity="info",
        )
        act = Finding(
            path="tests/test_x.py", line=1, end_line=2,
            rule="verification-act-assert", message="", severity="info",
        )
        untouched = Finding(
            path="tests/test_x.py", line=4, end_line=5,
            rule="pytest-test-function", message="", severity="info",
        )
        # act covers the changed function; the unchanged one is out of
        # scope even without an act.
        assert adapter.no_verification_findings([covered, act, untouched], diff, tmp_path) == []

        # an act belonging to a *different* function does not cover it
        other_act = Finding(
            path="tests/test_x.py", line=4, end_line=5,
            rule="verification-act-assert", message="", severity="info",
        )
        out = adapter.no_verification_findings([covered, other_act], diff, tmp_path)
        assert len(out) == 1
        assert out[0].symbol == "test_covered"

    def test_act_in_a_different_file_does_not_cover(self, adapter, tmp_path):
        DiffModel, Finding = adapter.DiffModel, adapter.Finding

        diff = DiffModel(
            changed_files=frozenset({"tests/test_a.py"}),
            added_lines={"tests/test_a.py": [(1, 3)]},
        )
        fn = Finding(
            path="tests/test_a.py", line=1, end_line=3,
            rule="pytest-test-function", message="", severity="info",
        )
        act_other_file = Finding(
            path="tests/test_b.py", line=1, end_line=3,
            rule="verification-act-assert", message="", severity="info",
        )
        assert len(adapter.no_verification_findings([fn, act_other_file], diff, tmp_path)) == 1
