"""pyscn adapter self-tests (v5.1 §18 rows: circular import, dead code).

Real scratch repositories, real merge base, the real CLI (distribution
spec §8). The §10.1 option B cases:

  1. a cycle introduced on the feature branch → cycles gate fails,
     mechanism absolute, exit 1;
  2. a legacy cycle allowlisted at BASE passes; a second, new cycle
     fails the gate on its own;
  3. the same cycle reported from another member matches the same
     allowlist entry — canonicalisation is rotation-invariant but
     direction-sensitive (§10.1);
  4. unreachable code added → deadcode gate fails via the per-type
     ratchet. Measured: pyscn 1.29.1's dead-code analysis is CFG-based
     and never reports merely-uncalled functions (verified with
     --min-severity info), so the violation is unreachable code inside
     a function.

Parser units run against JSON recorded from the pinned pyscn 1.29.1
(see the adapter's module docstring for how it was measured).
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from aufsicht.adapters.pyscn import (
    _module_file,
    _report_path,
    extract_cycle_rings,
    parse_deadcode_findings,
)
from aufsicht.allowlist import canonicalize_cycle
from tests.fixtures.scratch import commit, make_repo

# Recorded verbatim from the pinned pyscn 1.29.1 `analyze --json
# --select deps` on a src/scratch tree whose ring is c1→c3→c2→c1:
# Modules[] comes out SORTED; the direction lives in the edges.
RECORDED_CYCLE = {
    "system": {
        "DependencyAnalysis": {
            "CircularDependencies": {
                "TotalCycles": 1,
                "CircularDependencies": [
                    {
                        "Modules": ["scratch.c1", "scratch.c2", "scratch.c3"],
                        "Dependencies": [
                            {"From": "scratch.c1", "To": "scratch.c3", "Length": 1},
                            {"From": "scratch.c2", "To": "scratch.c1", "Length": 1},
                            {"From": "scratch.c3", "To": "scratch.c2", "Length": 1},
                        ],
                        "Severity": "low",
                        "Size": 3,
                    }
                ],
            }
        },
        "Summary": {"CyclicDependencies": 3},  # EDGES, not cycles — never used
    }
}

# Recorded verbatim from `analyze --json --select deadcode`: files is
# null when nothing was found, findings carry reason/severity/location.
RECORDED_DEADCODE = {
    "dead_code": {
        "files": [
            {
                "file_path": "src/scratch/unreach.py",
                "functions": [
                    {
                        "name": "f",
                        "findings": [
                            {
                                "location": {
                                    "file_path": "src/scratch/unreach.py",
                                    "start_line": 4,
                                    "end_line": 4,
                                    "start_column": 0,
                                    "end_column": 0,
                                },
                                "function_name": "f",
                                "reason": "unreachable_after_return",
                                "severity": "critical",
                                "description": "Code appears after a return "
                                               "statement and will never be executed",
                            }
                        ],
                    }
                ],
            }
        ],
    }
}

RECORDED_STDOUT = (
    "📊 Unified JSON report generated: /tmp/odd.dir name/repo/"
    ".pyscn/reports/analyze_20260816_224713.json\n"
)


def run_full(repo: Path) -> tuple[int, dict]:
    from tests.conftest import run_cli

    proc = run_cli("full", cwd=repo)
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            f"quality-full produced no JSON report.\nstdout: {proc.stdout[:500]}\n"
            f"stderr: {proc.stderr[:2000]}"
        )
    return proc.returncode, report


def _cycle_modules() -> dict[str, str]:
    """A directed ring a→b→a as scratch source files."""
    return {
        "src/scratch/legacy_a.py": (
            "from scratch.legacy_b import b_func\n"
            "\n"
            "\n"
            "def a_func() -> int:\n"
            "    return b_func()\n"
        ),
        "src/scratch/legacy_b.py": (
            "from scratch.legacy_a import a_func\n"
            "\n"
            "\n"
            "def b_func() -> int:\n"
            "    return a_func()\n"
        ),
    }


def _allowlist_toml(digest: str, reason: str) -> str:
    added_on = (dt.date.today() - dt.timedelta(days=5)).isoformat()
    expires = (dt.date.today() + dt.timedelta(days=30)).isoformat()
    return (
        "[[entry]]\n"
        f'rule = "cycle/{digest[:16]}"\n'
        f'reason = "{reason}"\n'
        'added_by = "human"\n'
        f"added_on = \"{added_on}\"\n"
        f"expires = \"{expires}\"\n"
    )


class TestParserUnits:
    """Against JSON recorded from the pinned pyscn 1.29.1."""

    def test_ring_is_rebuilt_from_edges_not_sorted_modules(self):
        rings = extract_cycle_rings(RECORDED_CYCLE)
        assert rings == [["scratch.c1", "scratch.c3", "scratch.c2"]]

    def test_no_cycles_is_null_not_empty(self):
        assert extract_cycle_rings({"system": {"DependencyAnalysis": {
            "CircularDependencies": {"CircularDependencies": None}}}}) == []
        assert extract_cycle_rings({}) == []

    def test_deadcode_finding_fields(self):
        findings = parse_deadcode_findings(RECORDED_DEADCODE)
        assert len(findings) == 1
        f = findings[0]
        assert f.path == "src/scratch/unreach.py"
        assert f.line == 4
        assert f.rule == "unreachable_after_return"
        assert f.severity == "critical"
        assert f.symbol == "f"
        assert "return" in f.message

    def test_deadcode_files_null_is_no_findings(self):
        assert parse_deadcode_findings({"dead_code": {"files": None}}) == []

    def test_report_path_handles_spaces_in_the_repo_path(self):
        path = _report_path(RECORDED_STDOUT, Path("/cwd"))
        assert path == Path(
            "/tmp/odd.dir name/repo/.pyscn/reports/analyze_20260816_224713.json"
        )

    def test_report_path_relative_is_joined_with_cwd(self):
        path = _report_path(
            "report generated: .pyscn/reports/analyze_1.json\n", Path("/cwd")
        )
        assert path == Path("/cwd/.pyscn/reports/analyze_1.json")

    def test_module_file_resolves_src_layout(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / "src" / "scratch").mkdir(parents=True)
        (repo / "src" / "scratch" / "app.py").write_text("x = 1\n")
        assert _module_file(repo, "scratch.app") == "src/scratch/app.py"
        assert _module_file(repo, "scratch.nowhere") is None


class TestCanonicalisation:
    """§10.1: rotate to the smallest member, keep direction, hash."""

    def test_rotation_is_the_same_cycle(self):
        forward = canonicalize_cycle(["scratch.m1", "scratch.m2", "scratch.m3"])
        rotated = canonicalize_cycle(["scratch.m2", "scratch.m3", "scratch.m1"])
        assert forward[0] == rotated[0] == ("scratch.m1", "scratch.m2", "scratch.m3")
        assert forward[1] == rotated[1]

    def test_reversed_direction_is_a_different_cycle(self):
        forward = canonicalize_cycle(["scratch.m1", "scratch.m2", "scratch.m3"])
        backward = canonicalize_cycle(["scratch.m1", "scratch.m3", "scratch.m2"])
        assert forward[0] == ("scratch.m1", "scratch.m2", "scratch.m3")
        assert backward[0] == ("scratch.m1", "scratch.m3", "scratch.m2")
        assert forward[1] != backward[1]


class TestCyclesAbsolute:
    def test_circular_import_introduced_fails_absolute(self, tmp_path):
        # §18: circular import introduced → pyscn, absolute, exit 1
        repo = make_repo(tmp_path / "cycle")
        commit(repo, {
            "src/scratch/a.py": (
                "from scratch.b import b_func\n"
                "\n"
                "\n"
                "def a_func() -> int:\n"
                "    return b_func()\n"
            ),
            "src/scratch/b.py": (
                "from scratch.a import a_func\n"
                "\n"
                "\n"
                "def b_func() -> int:\n"
                "    return a_func()\n"
            ),
        }, "introduce a<->b cycle", branch="feature")
        code, report = run_full(repo)
        assert code == 1, json.dumps(report, indent=2)
        entry = report["gates"]["cycles"]
        assert entry["status"] == "fail"
        assert entry["mechanism"] == "absolute"
        rules = {f["rule"] for f in report["findings"] if f["gate"] == "cycles"}
        assert rules == {"cycle"}
        assert entry["cycles"] == {
            "total": 1, "allowlisted": 0, "unallowlisted": 1,
        }

    def test_clean_scratch_repo_has_no_cycles(self, tmp_path):
        repo = make_repo(tmp_path / "clean")
        code, report = run_full(repo)
        assert report["gates"]["cycles"]["status"] == "pass"
        assert report["gates"]["cycles"]["cycles"]["unallowlisted"] == 0

    def test_allowlisted_legacy_cycle_passes_second_cycle_fails(self, tmp_path):
        # §10.1 option B: subtract approved cycles, fail on the remainder.
        # The allowlist is COMMITTED AT BASE (with the legacy cycle) so the
        # protected path .quality/allowlist.toml is not touched by the
        # feature branch (§11.2).
        repo = make_repo(tmp_path / "allowlisted")
        digest = canonicalize_cycle(["scratch.legacy_a", "scratch.legacy_b"])[1]
        commit(repo, {
            **_cycle_modules(),
            ".quality/allowlist.toml": _allowlist_toml(
                digest,
                "Legacy import ring predating the guardrails; tracked for removal",
            ),
        }, "legacy cycle, allowlisted at base")
        commit(repo, {
            "src/scratch/new_c.py": (
                "from scratch.new_d import d_func\n"
                "\n"
                "\n"
                "def c_func() -> int:\n"
                "    return d_func()\n"
            ),
            "src/scratch/new_d.py": (
                "from scratch.new_c import c_func\n"
                "\n"
                "\n"
                "def d_func() -> int:\n"
                "    return c_func()\n"
            ),
        }, "add a second cycle", branch="feature")
        code, report = run_full(repo)
        entry = report["gates"]["cycles"]
        assert entry["status"] == "fail"
        assert entry["mechanism"] == "absolute"
        assert entry["cycles"] == {
            "total": 2, "allowlisted": 1, "unallowlisted": 1,
        }
        # Only the new ring fails; the allowlisted legacy ring never does.
        messages = [f["detail"] for f in report["findings"] if f["gate"] == "cycles"]
        assert len(messages) == 1
        assert "scratch.new_c" in messages[0] and "scratch.new_d" in messages[0]
        assert "legacy" not in messages[0]
        # The allowlist change is at BASE, not in this PR.
        assert report["gates"]["integrity"]["status"] == "pass"
        assert code == 1

    def test_rotated_serialisation_matches_the_same_entry(self, tmp_path):
        # §10.1: "the same cycle reported starting from any member". The
        # allowlist entry is generated from a ROTATED serialisation of the
        # ring; the gate's canonicalisation must still match it.
        ring = {
            "src/scratch/m1.py": (
                "from scratch.m2 import m2\n"
                "\n"
                "\n"
                "def m1() -> int:\n"
                "    return m2()\n"
            ),
            "src/scratch/m2.py": (
                "from scratch.m3 import m3\n"
                "\n"
                "\n"
                "def m2() -> int:\n"
                "    return m3()\n"
            ),
            "src/scratch/m3.py": (
                "from scratch.m1 import m1\n"
                "\n"
                "\n"
                "def m3() -> int:\n"
                "    return m1()\n"
            ),
        }
        repo = make_repo(tmp_path / "rotated")
        # Entry built from [m2, m3, m1] — the same directed ring, entered
        # from a different member.
        rotated_digest = canonicalize_cycle(
            ["scratch.m2", "scratch.m3", "scratch.m1"]
        )[1]
        commit(repo, {
            **ring,
            ".quality/allowlist.toml": _allowlist_toml(
                rotated_digest,
                "Legacy three-module ring entered from another member",
            ),
        }, "legacy ring, allowlisted via rotated digest")
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
        }, "unrelated change on top", branch="feature")
        code, report = run_full(repo)
        entry = report["gates"]["cycles"]
        assert entry["status"] == "pass", entry
        assert entry["cycles"] == {
            "total": 1, "allowlisted": 1, "unallowlisted": 0,
        }

    def test_reversed_direction_does_not_match_the_entry(self, tmp_path):
        # The flip side of "keep direction": an entry generated from the
        # sorted member set does not cover the reversed ring.
        reversed_ring = {
            "src/scratch/m1.py": (
                "from scratch.m3 import m3\n"
                "\n"
                "\n"
                "def m1() -> int:\n"
                "    return m3()\n"
            ),
            "src/scratch/m3.py": (
                "from scratch.m2 import m2\n"
                "\n"
                "\n"
                "def m3() -> int:\n"
                "    return m2()\n"
            ),
            "src/scratch/m2.py": (
                "from scratch.m1 import m1\n"
                "\n"
                "\n"
                "def m2() -> int:\n"
                "    return m1()\n"
            ),
        }
        repo = make_repo(tmp_path / "reversed")
        # A naive member-set entry (what a direction-blind adapter would
        # write): canonicalise the SORTED modules [m1, m2, m3].
        sorted_digest = canonicalize_cycle(
            ["scratch.m1", "scratch.m2", "scratch.m3"]
        )[1]
        commit(repo, {
            **reversed_ring,
            ".quality/allowlist.toml": _allowlist_toml(
                sorted_digest,
                "Member-set entry that must not match the reversed ring",
            ),
        }, "reversed ring with a member-set entry")
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
        }, "unrelated change on top", branch="feature")
        code, report = run_full(repo)
        entry = report["gates"]["cycles"]
        assert entry["status"] == "fail", entry
        assert entry["cycles"]["unallowlisted"] == 1
        message = [f["detail"] for f in report["findings"]
                   if f["gate"] == "cycles"][0]
        # The true ring m1→m3→m2→m1, direction kept.
        assert "m1 → scratch.m3 → scratch.m2 → scratch.m1" in message


class TestReadOnlyContract:
    def test_full_run_leaves_no_pyscn_scratch_output(self, tmp_path):
        # `full` is read-only (v5.1 §3 command set): pyscn scratch-writes
        # <repo>/.pyscn next to the analysed code, and the adapter must
        # remove exactly what it created. A pyscn-created directory after
        # a read-only command is a mutation — it breaks `init`'s
        # clean-tree refusals and any workflow that commits after gating.
        # (Scoped to .pyscn: other gates own their own scratch output.)
        repo = make_repo(tmp_path / "readonly")
        commit(repo, {
            "src/scratch/a.py": (
                "from scratch.b import b_func\n"
                "\n"
                "\n"
                "def a_func() -> int:\n"
                "    return b_func()\n"
            ),
            "src/scratch/b.py": (
                "from scratch.a import a_func\n"
                "\n"
                "\n"
                "def b_func() -> int:\n"
                "    return a_func()\n"
            ),
        }, "cycle, and gate scratch output must vanish", branch="feature")
        code, report = run_full(repo)
        assert report["gates"]["cycles"]["status"] == "fail"
        assert not (repo / ".pyscn").exists()


class TestDeadCodeRatchet:
    def test_unreachable_code_added_fails_per_type_ratchet(self, tmp_path):
        # §18: unreachable function added → pyscn, ratchet. Measured:
        # pyscn 1.29.1 reports code after return/raise inside a function
        # (reason "unreachable_after_return", severity "critical"); it
        # does not detect merely-uncalled functions.
        repo = make_repo(tmp_path / "dead")
        commit(repo, {
            "src/scratch/unreach.py": (
                "def broken(x: int) -> int:\n"
                "    if x > 0:\n"
                "        return 1\n"
                "        print(\"never runs\")\n"
                "    return 0\n"
            ),
        }, "add unreachable code", branch="feature")
        code, report = run_full(repo)
        assert code == 1, json.dumps(report, indent=2)
        entry = report["gates"]["deadcode"]
        assert entry["status"] == "fail"
        assert entry["mechanism"] == "per-rule-ratchet"
        rules = {f["rule"] for f in report["findings"] if f["gate"] == "deadcode"}
        assert rules == {"pyscn/unreachable_after_return"}
        assert entry["ratchet"]["regressed_rules"] == [
            {"rule": "unreachable_after_return", "base": 0, "head": 1}
        ]

    def test_preexisting_unreachable_code_ratchets_clean(self, tmp_path):
        # The ratchet tolerates what the merge base already had (§4.3).
        repo = make_repo(tmp_path / "preexisting")
        commit(repo, {
            "src/scratch/unreach.py": (
                "def broken(x: int) -> int:\n"
                "    if x > 0:\n"
                "        return 1\n"
                "        print(\"never runs\")\n"
                "    return 0\n"
            ),
        }, "legacy unreachable code at base")
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
        entry = report["gates"]["deadcode"]
        assert entry["status"] == "pass", entry
        assert entry["ratchet"]["totals"] == {"base": 1, "head": 1}

    def test_uncalled_function_alone_does_not_fail(self, tmp_path):
        # Documents the measured boundary of pyscn's dead-code analysis:
        # a never-called function is NOT a finding (CFG analysis only).
        # If a future pyscn starts reporting it, this test flips and the
        # ratchet covers it — either way the gate behaves.
        repo = make_repo(tmp_path / "uncalled")
        commit(repo, {
            "src/scratch/lonely.py": (
                "def lonely(x: int) -> int:\n"
                "    \"\"\"Never called from anywhere.\"\"\"\n"
                "    return x * 2\n"
            ),
        }, "add uncalled function", branch="feature")
        code, report = run_full(repo)
        entry = report["gates"]["deadcode"]
        assert entry["status"] == "pass", entry
        assert entry["ratchet"]["totals"] == {"base": 0, "head": 0}
