"""Xenon adapter self-tests (v5.1 §6.2, §8 last row).

Xenon gates the AGGREGATE: the ratcheted integer is the count of modules
whose average block complexity ranks above --max-modules. The two cases
that matter:

  1. aggregate drift — nothing trips C901 per-function, but a new module
     pushes the modules-over count past the merge base → count-ratchet
     failure, exit 1;
  2. a simple added function passes both complexity gates (C901 gates
     per-function, xenon the aggregate — they must not double-block).

Parser units run against stderr recorded from the pinned xenon 0.9.3
(see the adapter's module docstring for how it was measured).
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from aufsicht.adapters.xenon import (
    MODULE_OVER,
    base_xenon_letters,
    parse_modules_over,
    rank_range,
    xenon_gate,
)
from aufsicht.errors import ToolingError
from aufsicht.pipeline import MODE_FULL, build_context
from tests.conftest import run_git
from tests.fixtures.scratch import commit, make_repo
from tests.test_selftests import run_full

# Recorded verbatim from the pinned xenon 0.9.3 probing a tree of
# modules with known function complexities (-a A -m A -b C). Block and
# global-average infraction lines are present on purpose: only module
# lines may contribute to the count.
RECORDED_STDERR = "\n".join(
    [
        'ERROR:xenon:block "src/block_25.py:3 f0" has a rank of D',
        'ERROR:xenon:block "src/avg_40_5.py:3 f0" has a rank of F',
        "ERROR:xenon:average complexity is ranked C",
        "ERROR:xenon:module 'src/block_25.py' has a rank of B",
        "ERROR:xenon:module 'src/avg_40_5.py' has a rank of F",
        "ERROR:xenon:module 'src/avg_30_5.py' has a rank of E",
        "ERROR:xenon:module 'src/avg_20_5.py' has a rank of D",
        "ERROR:xenon:module 'src/avg_20_0.py' has a rank of C",
        "ERROR:xenon:module 'src/avg_10_5.py' has a rank of C",
        "ERROR:xenon:module 'src/avg_10_0.py' has a rank of B",
        "ERROR:xenon:module 'src/avg_5_5.py' has a rank of B",
    ]
)


def _function(name: str, complexity: int) -> str:
    """A function of exact radon cyclomatic *complexity* (base 1 + one
    `if` each; measured against xenon 0.9.3 in the probe)."""
    lines = [f"def {name}(x):"]
    for b in range(complexity - 1):
        lines += [f"    if x == {b}:", f"        return {b}"]
    lines.append("    return -1")
    return "\n".join(lines)


def _module(functions: list[tuple[str, int]]) -> str:
    return "\n\n\n".join(_function(n, c) for n, c in functions) + "\n"


class TestParser:
    def test_counts_only_module_lines(self):
        over = parse_modules_over(RECORDED_STDERR)
        assert len(over) == 8  # 2 block lines + 1 average line excluded
        assert ("src/avg_5_5.py", "B") in over
        assert ("src/avg_40_5.py", "F") in over

    def test_clean_output_counts_zero(self):
        assert parse_modules_over("") == []

    def test_unparseable_module_fails_closed(self):
        stderr = (
            "WARNING:xenon:cannot parse src/broken.py: invalid syntax (<unknown>, line 1)\n"
            "ERROR:xenon:module 'src/other.py' has a rank of B\n"
        )
        with pytest.raises(ToolingError, match="could not parse"):
            parse_modules_over(stderr)

    def test_measured_rank_scale_wording(self):
        # Boundaries measured with crafted modules (avg 5.0 → A,
        # 5.5 → B, 10.0 → B, 10.5 → C, ... 40.5 → F).
        assert rank_range("A") == "[0, 5]"
        assert rank_range("B") == "(5, 10]"
        assert rank_range("C") == "(10, 20]"
        assert rank_range("F") == "(40, ∞)"


class TestLetters:
    def test_base_letters_read_from_base_commit(self, tmp_path):
        repo = make_repo(tmp_path / "letters")
        base_sha = run_git("rev-parse", "main", cwd=repo).stdout.strip()
        assert base_xenon_letters(repo, base_sha) == ("A", "A", "C")

        config = (repo / ".quality" / "config.toml").read_text(encoding="utf-8")
        config = config.replace('xenon_max_modules = "A"', 'xenon_max_modules = "B"')
        commit(
            repo,
            {".quality/config.toml": config},
            "loosen module rank",
            branch="feature",
        )
        head_sha = run_git("rev-parse", "HEAD", cwd=repo).stdout.strip()
        # §4.3: BASE source is analysed under BASE's configuration —
        # the letters come from the commit being read, not HEAD's.
        assert base_xenon_letters(repo, base_sha) == ("A", "A", "C")
        assert base_xenon_letters(repo, head_sha) == ("A", "B", "C")

    def test_missing_base_config_falls_back_to_defaults(self, tmp_path):
        repo = make_repo(tmp_path / "noconfig")
        # A sha that predates .quality/config.toml entirely.
        assert base_xenon_letters(repo, "0" * 40) == ("A", "A", "C")


class TestAggregateComplexityRatchet:
    def test_new_swamp_module_fails_count_ratchet(self, tmp_path):
        # §8 last row: modules over Xenon --max-modules > base. Three
        # functions of complexity 9 (8 branches each): every one is
        # below C901's threshold of 10, the module average is 9 → rank
        # B > A. Per-function gates see nothing; the aggregate does.
        repo = make_repo(tmp_path / "agg")
        swamp = _module([("alpha", 9), ("beta", 9), ("gamma", 9)])
        commit(
            repo, {"src/scratch/swamp.py": swamp}, "add swamp module", branch="feature"
        )

        code, report = run_full(repo)
        assert code == 1, report
        gate = report["gates"]["xenon"]
        assert gate["status"] == "fail"
        assert gate["mechanism"] == "count-ratchet"
        assert gate["base"] == 0
        assert gate["head"] == 1
        regressed = gate["ratchet"]["regressed_rules"]
        assert regressed == [{"rule": MODULE_OVER, "base": 0, "head": 1}]
        findings = [f for f in report["findings"] if f["gate"] == "xenon"]
        assert [(f["path"], f["rule"]) for f in findings] == [
            ("src/scratch/swamp.py", MODULE_OVER)
        ]
        # The drift is invisible to the per-function gate — that is the
        # whole reason the aggregate ratchet exists (v5.1 §6.2).
        assert report["gates"]["complexity"]["status"] == "pass"

    def test_simple_added_function_passes_both_complexity_gates(self, tmp_path):
        # C901 (per-function) and xenon (aggregate) must not double-block
        # a trivial change: one added function of complexity 1.
        repo = make_repo(tmp_path / "clean-add")
        commit(
            repo,
            {
                "src/scratch/extra.py": "def triple(x: int) -> int:\n    return 3 * x\n",
            },
            "add a simple function",
            branch="feature",
        )

        code, report = run_full(repo)
        assert code == 0, report
        xenon = report["gates"]["xenon"]
        assert xenon["status"] == "pass"
        assert xenon["base"] == 0 and xenon["head"] == 0
        assert report["gates"]["complexity"]["status"] == "pass"

    def test_clean_repo_ratchet_counts_zero(self, tmp_path):
        repo = make_repo(tmp_path / "clean")
        code, report = run_full(repo)
        assert code == 0, report
        xenon = report["gates"]["xenon"]
        assert xenon["status"] == "pass"
        assert xenon["ratchet"]["totals"] == {"base": 0, "head": 0}


class TestGateContract:
    def _ctx(self, tmp_path, name):
        repo = make_repo(tmp_path / name)
        return repo, build_context(repo, MODE_FULL)

    def test_exemption_is_visible_not_silent(self, tmp_path):
        # v5.1 §4.5/§15: an exemption not visible in the report is
        # indistinguishable from a pass — status "exempt" + reason.
        _repo, ctx = self._ctx(tmp_path, "exempt")
        ctx.report.exempt_tools = ["xenon"]
        ctx.report.exemption_reasons = {"xenon": "pin changed in this PR"}
        result = xenon_gate(ctx)
        assert result.status == "exempt"
        assert result.extra["ratchet"] == "exempt"
        assert result.extra["ratchet_reason"] == "pin changed in this PR"

    def test_fast_mode_skips(self, tmp_path):
        _repo, ctx = self._ctx(tmp_path, "fast")
        ctx.mode = "fast"
        result = xenon_gate(ctx)
        assert result.status == "skipped"

    def test_invalid_letter_fails_closed(self, tmp_path):
        _repo, ctx = self._ctx(tmp_path, "badletter")
        ctx.config = replace(ctx.config, xenon_max_modules="Z")
        with pytest.raises(ToolingError, match="xenon_max_modules"):
            xenon_gate(ctx)
