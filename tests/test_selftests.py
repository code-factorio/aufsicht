"""v5.1 §18 self-test table, executed (distribution spec §8).

Each case: a generated scratch repository with a real merge base, a
real violation applied, the real CLI invoked. Each asserts:

    1. the expected gate fails
    2. via the expected mechanism (absolute / diff-scoped / per-rule ratchet)
    3. with the expected exit code (v5.1 §15)

plus the negative: quality-full on the clean copy passes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import run_cli
from tests.fixtures.scratch import commit, edit, make_repo


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


def assert_gate_failure(report: dict, gate: str, mechanism: str | None, exit_code: int, returncode: int):
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


class TestCleanCopyPasses:
    """The negative case every §18 case is measured against."""

    def test_clean_scratch_repo_passes_quality_full(self, tmp_path):
        repo = make_repo(tmp_path / "clean")
        code, report = run_full(repo)
        assert code == 0, json.dumps(report, indent=2)
        assert report["status"] == "pass"
        assert report["base"]["sha"]
        assert report["runner_version"]
        assert report["spec_version"] == "v5.1"

    def test_report_carries_runner_version_matching_the_tool(self, tmp_path):
        # dist §8: "report carries runner version → present and matches
        # the installed tool"
        from aufsicht import __version__

        repo = make_repo(tmp_path / "version")
        code, report = run_full(repo)
        assert report["runner_version"] == __version__
        lock_runner = None
        for line in (repo / ".quality" / "toolchain.lock").read_text().splitlines():
            if line.startswith("runner_version"):
                lock_runner = line.split("=")[1].strip().strip('"')
        assert lock_runner == __version__


class TestRuffDiffScoped:
    def test_formatting_violation(self, tmp_path):
        # §18: formatting violation → Ruff, diff-scoped
        repo = make_repo(tmp_path / "fmt")
        commit(repo, {
            "src/scratch/app.py": "def add(a: int, b: int) -> int:\n    return a+b\n\n\ndef greet(name: str) -> str:\n    return f\"hello {name}\"\n",
        }, "unformatted", branch="feature")
        code, report = run_full(repo)
        entry = assert_gate_failure(report, "ruff", "diff-scoped", 1, code)
        rules = {f["rule"] for f in report["findings"] if f["gate"] == "ruff"}
        assert "format" in rules or any(r.startswith("E") for r in rules), rules

    def test_eval_added_fails_ruff_s(self, tmp_path):
        # §18: eval() added → Ruff S307, diff-scoped
        repo = make_repo(tmp_path / "eval")
        commit(repo, {
            "src/scratch/app.py": (
                "def add(a: int, b: int) -> int:\n"
                "    return a + b\n"
                "\n"
                "\n"
                "def compute(expr: str) -> int:\n"
                "    return eval(expr)\n"
            ),
        }, "add eval", branch="feature")
        code, report = run_full(repo)
        assert_gate_failure(report, "ruff-s", "diff-scoped", 1, code)
        rules = {f["rule"] for f in report["findings"] if f["gate"] == "ruff-s"}
        assert "S307" in rules, rules

    def test_complexity_15_function(self, tmp_path):
        # §18: complexity 15 function → Ruff C901, diff-scoped
        # (changed-FILE scope — probe_facts.C901_SCOPE)
        body = "def complex_function(x):\n" + "".join(
            f"    if x == {i}:\n        return {i}\n" for i in range(1, 16)
        ) + "    return 0\n"
        repo = make_repo(tmp_path / "c901")
        commit(repo, {
            "src/scratch/complex.py": body,
            "tests/test_complex.py": (
                "from scratch.complex import complex_function\n"
                "\n"
                "\n"
                "def test_complex_function():\n"
                "    assert complex_function(1) == 1\n"
            ),
        }, "add complex function", branch="feature")
        code, report = run_full(repo)
        entry = assert_gate_failure(report, "complexity", "diff-scoped", 1, code)
        assert entry["detail"], "failure must carry an actionable detail (v5.1 §15)"
        rules = {f["rule"] for f in report["findings"] if f["gate"] == "complexity"}
        assert rules == {"C901"}, rules

    def test_legacy_complexity_in_unchanged_file_not_diff_scoped(self, tmp_path):
        # The flip side of changed-file scope: a legacy C901 in a file
        # nobody edited is the ratchet's subject, not the diff gate's.
        legacy = "def legacy_complex(x):\n" + "".join(
            f"    if x == {i}:\n        return {i}\n" for i in range(1, 14)
        ) + "    return 0\n"
        repo = make_repo(tmp_path / "legacy")
        commit(repo, {"src/scratch/legacy.py": legacy}, "add legacy module")
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
        assert report["gates"]["complexity"]["status"] == "pass", report["gates"]["complexity"]


class TestSuppressionScan:
    def test_type_ignore_on_added_line(self, tmp_path):
        # §18: "# type: ignore on an added line → added-line scan / PGH003"
        repo = make_repo(tmp_path / "typeignore")
        commit(repo, {
            "src/scratch/app.py": (
                "def add(a: int, b: int) -> int:\n"
                "    return a + b  # type: ignore\n"
                "\n"
                "\n"
                "def greet(name: str) -> str:\n"
                "    return f\"hello {name}\"\n"
            ),
        }, "suppress", branch="feature")
        code, report = run_full(repo)
        # The gate is named "suppressions" in this runner; mechanism is
        # diff-scoped per the v5.1 §3 table (added-line scan + PGH).
        entry = report["gates"].get("suppressions")
        assert entry and entry["status"] == "fail", report["gates"]
        assert "diff-scoped" in entry["mechanism"]
        rules = {f["rule"] for f in report["findings"] if f["gate"] == "suppressions"}
        assert any("type-ignore" in r or "PGH" in r for r in rules), rules
        assert code == 1

    def test_legacy_type_ignore_not_flagged(self, tmp_path):
        repo = make_repo(tmp_path / "legacy-suppress", app=(
            "def add(a: int, b: int) -> int:\n"
            "    return a + b  # type: ignore\n"
            "\n"
            "\n"
            "def greet(name: str) -> str:\n"
            "    return f\"hello {name}\"\n"
        ))
        commit(repo, {
            "src/scratch/app.py": (
                "def add(a: int, b: int) -> int:\n"
                "    return a + b  # type: ignore\n"
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
        assert report["gates"]["suppressions"]["status"] == "pass", report["gates"]["suppressions"]
