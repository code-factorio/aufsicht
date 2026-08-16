"""Addendum §9 self-tests — present from day one, SKIPPED until the
Tier 3 mutation gate is built (distribution spec §8: the tests exist
before the gate does; they must not silently pass).

The addendum is frozen as "do not implement before Tier 1 has been in
daily use". When Tier 3 lands, replace each pytest.skip with the real
scratch-repo case per addendum §9, asserting gate, mechanism and exit
code exactly like tests/test_selftests.py does for §18.
"""

from __future__ import annotations

import pytest

TIER3_BUILT = False  # flip when the mutation gate ships

requires_tier3 = pytest.mark.skipif(
    not TIER3_BUILT,
    reason="Tier 3 mutation gate not built (addendum: implement only "
           "after Tier 1 has been in daily use)",
)


@requires_tier3
class TestMutationScopeAndScore:
    def test_weak_test_present_boundary_mutant_reported(self):
        """§9: weak test present → quality-mutation FAIL, boundary
        mutant reported (is_adult, age >= 18 → age > 18 survives)."""

    def test_boundary_test_added_passes(self):
        """§9: add test_exact_adult_boundary(18) → quality-mutation PASS."""

    def test_pragma_no_mutate_forms_covered(self):
        """§9: '# pragma: no mutate' on an added line (single, block,
        start/end forms) → suppression scan FAIL."""

    def test_mutmut_config_change_fires_integrity(self):
        """§9: edit to [tool.mutmut] in pyproject → integrity FAIL via
        semantic section hash; reformat-only does NOT fire."""

    def test_small_scoreable_does_not_fail(self):
        """§9: scoreable = 6, 3 survive → reported, does NOT fail (min
        denominator 20)."""

    def test_no_tests_in_denominator_flagged_not_failed(self):
        """§9: any no_tests > 0 → no_tests_flagged true, warning."""

    def test_incomplete_run_exits_3_no_score(self):
        """§9: 'not checked' / interrupted run → exit 3, no score."""

    def test_report_only_threshold_failure_exits_0(self):
        """§9: report-only + threshold failure → status 'fail',
        would_block true, CI exit 0."""

    def test_report_only_broken_platform_exits_3(self):
        """§9: report-only + broken platform → status 'tooling-error',
        exit 3 (NOT swallowed)."""

    def test_type_check_command_unset_uses_public_export(self):
        """§9: adapter runs off export-cicd-stats, no internals read."""

    def test_survivor_fixed_by_simplifying_code_passes(self):
        """§9: survivor fixed by legitimately simplifying production
        code → gate passes, no evasion flagged."""

    def test_docs_only_change_reports_skipped(self):
        """§9: docs-only change → status 'skipped', not 'pass'."""

    def test_unsupported_platform_exits_3_not_skipped(self):
        """§9: Windows runner without WSL → platform 'unsupported',
        exit 3, NOT 'skipped'."""

    def test_function_cap_sampling_deterministic(self):
        """§9: function count over cap → sampled: true, same targets on
        rerun (SHA-256 hash-and-sort, not a seeded PRNG)."""

    def test_wall_clock_truncation_exits_3(self):
        """§9: wall-clock limit exceeded → truncated: true, exit 3, NOT
        a pass."""

    def test_whole_function_targeted_reported_as_functions(self):
        """§9: changed one line of a 40-line function → whole function
        targeted; scope reported as functions_tested, not lines."""

    def test_suspicious_excluded_from_scoreable_reported(self):
        """§9: suspicious outcome present → excluded from scoreable,
        reported separately."""


@requires_tier3
class TestSymbolToTargetTranslation:
    """§9: changed symbol → generated target → exactly the intended
    mutant set, reconciled against mutmut's generated metadata — never
    an invented naming convention."""

    def test_top_level_function(self):
        ...

    def test_class_method(self):
        ...

    def test_async_function(self):
        ...

    def test_init_method(self):
        ...

    def test_nested_inner_function(self):
        ...

    def test_module_under_src_layout(self):
        ...
