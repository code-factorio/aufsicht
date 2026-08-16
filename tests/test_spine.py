"""Spine unit tests (distribution spec §12 step 1): the parts every
adapter leans on — diff parsing, the one scope filter, per-rule
comparison, base resolution, config loading, semantic hashing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aufsicht.config import QualityConfig
from aufsicht.diffmodel import parse_unified_diff
from aufsicht.errors import ToolingError
from aufsicht.integrity import canonicalize, is_protected, semantic_hash
from aufsicht.model import DiffModel, Finding, NO_RULE, ScopeMode
from aufsicht.ratchet import compare, count_by_rule
from aufsicht.scope import in_scope
from aufsicht.suppression import scan_diff
from tests.conftest import run_git
from tests.fixtures.scratch import make_repo


class TestParseUnifiedDiff:
    def test_simple_addition(self):
        text = (
            "diff --git a/src/app.py b/src/app.py\n"
            "index 111..222 100644\n"
            "--- a/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -3,0 +4,2 @@\n"
            "+x = 1\n"
            "+y = 2\n"
        )
        changed, added = parse_unified_diff(text)
        assert changed == {"src/app.py"}
        assert added == {"src/app.py": [(4, 2)]}

    def test_multiple_hunks_and_files(self):
        text = (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1,0 +2,1 @@\n"
            "+new\n"
            "@@ -10,1 +11,0 @@\n"
            "-old\n"
            "@@ -11,0 +12,2 @@\n"
            "+one\n"
            "+two\n"
            "diff --git a/b.py b/b.py\n"
            "--- a/b.py\n"
            "+++ b/b.py\n"
            "@@ -5,2 +5,0 @@\n"
            "-gone\n"
            "-gone2\n"
        )
        changed, added = parse_unified_diff(text)
        assert changed == {"a.py", "b.py"}
        assert added == {"a.py": [(2, 1), (12, 2)]}  # deletions add nothing
        assert "b.py" not in added

    def test_new_file_counts_from_one(self):
        text = (
            "diff --git a/new.py b/new.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/new.py\n"
            "@@ -0,0 +1,3 @@\n"
            "+a\n"
            "+b\n"
            "+c\n"
        )
        changed, added = parse_unified_diff(text)
        assert changed == {"new.py"}
        assert added == {"new.py": [(1, 3)]}

    def test_bad_hunk_header_raises_tooling_error(self):
        with pytest.raises(ToolingError):
            parse_unified_diff("+++ b/x.py\n@@ nonsense @@")


class TestScopeFilter:
    def _diff(self) -> DiffModel:
        return DiffModel(
            changed_files=frozenset({"a.py", "b.py"}),
            added_lines={"a.py": [(10, 3)], "b.py": [(1, 1)]},
        )

    def test_file_mode(self):
        diff = self._diff()
        assert in_scope(Finding(path="a.py", line=1, rule="X", message=""), diff, ScopeMode.FILE)
        assert not in_scope(Finding(path="c.py", line=1, rule="X", message=""), diff, ScopeMode.FILE)

    def test_line_mode_overlap(self):
        diff = self._diff()
        assert in_scope(Finding(path="a.py", line=12, rule="X", message=""), diff, ScopeMode.LINE)
        assert not in_scope(Finding(path="a.py", line=14, rule="X", message=""), diff, ScopeMode.LINE)
        assert not in_scope(Finding(path="c.py", line=10, rule="X", message=""), diff, ScopeMode.LINE)

    def test_line_mode_range_intersects(self):
        diff = self._diff()
        # span 8..11 intersects 10..12
        assert in_scope(
            Finding(path="a.py", line=8, end_line=11, rule="X", message=""),
            diff, ScopeMode.LINE,
        )

    def test_function_mode_uses_span(self):
        diff = self._diff()
        assert in_scope(
            Finding(path="a.py", line=10, end_line=10, rule="C901", message=""),
            diff, ScopeMode.FUNCTION,
        )
        assert not in_scope(
            Finding(path="a.py", line=5, end_line=5, rule="C901", message=""),
            diff, ScopeMode.FUNCTION,
        )


class TestRatchetCompare:
    def test_fixed_one_introduced_another_fails(self):
        # v5.1 §18: "fix one F401, add one B006 → per-rule ratchet
        # (a total ratchet passes this)"
        outcome = compare({"F401": 2, "B006": 0}, {"F401": 1, "B006": 1})
        assert not outcome.passed
        assert [(r.rule, r.base, r.head) for r in outcome.regressed] == [("B006", 0, 1)]
        assert outcome.totals == (2, 2)  # totals unchanged — still fails

    def test_rule_removed_in_head_passes_naturally(self):
        outcome = compare({"F401": 4}, {})
        assert outcome.passed  # HEAD[rule] = 0 <= BASE[rule]

    def test_no_rule_bucket(self):
        counts = count_by_rule([None, "ruleX", None])
        assert counts == {NO_RULE: 2, "ruleX": 1}

    def test_null_rules_ratchet_like_any_other(self):
        outcome = compare({NO_RULE: 1}, {NO_RULE: 2})
        assert not outcome.passed


class TestSemanticHash:
    def test_key_order_and_comments_are_cosmetic(self):
        a = {"tool": {"ruff": {"line_length": 100, "select": ["E", "F"]}}}
        b = {"tool": {"ruff": {"select": ["E", "F"], "line_length": 100}}}
        assert semantic_hash(a) == semantic_hash(b)

    def test_array_order_is_significant(self):
        a = {"select": ["E", "F"]}
        b = {"select": ["F", "E"]}
        assert semantic_hash(a) != semantic_hash(b)

    def test_scalar_types_differ(self):
        assert semantic_hash({"x": 1}) != semantic_hash({"x": 1.0})
        assert semantic_hash({"x": 1}) != semantic_hash({"x": "1"})

    def test_canonicalize_sorts_recursively(self):
        assert canonicalize({"b": {"z": 1, "a": 2}, "a": []}) == {"a": [], "b": {"a": 2, "z": 1}}


class TestProtectedPaths:
    def test_v5112_list(self):
        assert is_protected(".quality/config.toml")
        assert is_protected(".quality/semgrep/rules.yaml")
        assert is_protected(".quality/toolchain.lock")
        assert is_protected("pyrightconfig.json")
        assert is_protected(".pyscn.toml")
        assert is_protected(".pre-commit-config.yaml")
        assert is_protected(".github/workflows/ci.yml")
        assert is_protected("AGENTS.md")
        assert not is_protected("pyproject.toml")  # deliberately absent
        assert not is_protected("src/app.py")


class TestConfig:
    def test_defaults_from_minimal_file(self):
        cfg = QualityConfig.from_dict({"schema_version": 1})
        assert cfg.c901_max == 10
        assert cfg.fast_budget_seconds == 15.0
        assert cfg.integrity_model == "B"
        assert "pytest.raises" in cfg.verification_calls

    def test_newer_schema_refuses(self):
        with pytest.raises(ToolingError):
            QualityConfig.from_dict({"schema_version": 2})

    def test_older_schema_refuses(self):
        with pytest.raises(ToolingError):
            QualityConfig.from_dict({"schema_version": 0})

    def test_missing_schema_refuses(self):
        with pytest.raises(ToolingError):
            QualityConfig.from_dict({})

    def test_overrides(self):
        cfg = QualityConfig.from_dict({
            "schema_version": 1,
            "base": {"ref": "develop"},
            "tests": {"budget_seconds": 240},
        })
        assert cfg.base_ref == "develop"
        assert cfg.tests_budget_seconds == 240


class TestSuppressionScan:
    def test_added_line_suppressions_found(self, tmp_path: Path):
        (tmp_path / "a.py").write_text("x = 1  # type: ignore\ny = 2\n")
        diff = DiffModel(changed_files=frozenset({"a.py"}), added_lines={"a.py": [(1, 1)]})
        findings = scan_diff(tmp_path, diff)
        assert len(findings) == 1
        assert findings[0].rule == "suppression/type-ignore"
        assert findings[0].line == 1

    def test_pragma_no_mutate_all_forms(self, tmp_path: Path):
        (tmp_path / "b.py").write_text(
            "a = 1  # pragma: no mutate\n"
            "b = 2  # pragma: no mutate begin\n"
            "c = 3  # pragma: no mutate end\n"
        )
        diff = DiffModel(changed_files=frozenset({"b.py"}), added_lines={"b.py": [(1, 3)]})
        rules = {f.line: f.rule for f in scan_diff(tmp_path, diff)}
        assert set(rules.values()) == {"suppression/pragma-no-mutate"}

    def test_legacy_line_not_flagged(self, tmp_path: Path):
        (tmp_path / "a.py").write_text("x = 1  # type: ignore\ny = 2\n")
        diff = DiffModel(changed_files=frozenset({"a.py"}), added_lines={"a.py": [(2, 1)]})
        assert scan_diff(tmp_path, diff) == []


class TestBaseResolution:
    def test_config_ref_resolves_and_merges(self, tmp_path: Path):
        from aufsicht.base import resolve_base

        repo = make_repo(tmp_path / "repo")
        run_git("checkout", "-q", "-b", "feature", cwd=repo)
        (repo / "src/scratch/app.py").write_text(
            "def add(a: int, b: int) -> int:\n    return a - b\n", encoding="utf-8"
        )
        run_git("commit", "-q", "-am", "change", cwd=repo)
        cfg = QualityConfig.load(repo)
        base = resolve_base(repo, cfg)
        assert base.source == "config"
        assert base.sha  # merge-base of feature and main

    def test_shallow_clone_fails_closed(self, tmp_path: Path):
        from aufsicht.base import BaseResolutionError, resolve_base

        repo = make_repo(tmp_path / "repo")
        # Simulate shallowness: grafts make rev-parse report shallow.
        (repo / ".git/shallow").write_text(
            run_git("rev-parse", "HEAD", cwd=repo).stdout.strip(), encoding="utf-8"
        )
        cfg = QualityConfig.load(repo)
        with pytest.raises(BaseResolutionError) as exc_info:
            resolve_base(repo, cfg)
        assert "fetch-depth" in (exc_info.value.remedy or "")

    def test_unresolvable_base_fails_closed(self, tmp_path: Path):
        from aufsicht.base import BaseResolutionError, resolve_base

        repo = make_repo(tmp_path / "repo", with_quality=False)
        # No CI env (conftest clears them), no [base] ref, no remote → exit-3 path.
        with pytest.raises(BaseResolutionError):
            resolve_base(repo, QualityConfig.from_dict({"schema_version": 1}))
