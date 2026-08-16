"""Global probe compatibility self-tests (distribution spec §6, §13).

The probes were answered once by running the tools and became runner
code (probe_facts). These tests re-assert the answers against the
pinned analyzer versions, so a version bump that changes behaviour
fails loudly here rather than silently changing gate semantics.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from aufsicht import probe_facts
from aufsicht.errors import ToolingError


@pytest.fixture(scope="module")
def pinned_env(tmp_path_factory):
    from tests.fixtures.scratch import SCRATCH_TOOLCHAIN
    import aufsicht.toolchain as tc

    tmp = tmp_path_factory.mktemp("probe-locks")
    (tmp / "toolchain.lock").write_text(SCRATCH_TOOLCHAIN)
    lock = tc.load_toolchain(tmp)
    return tc.analyzer_env(lock)


class TestC901Probe:
    def test_pinned_ruff_still_spans_def_line_only(self, pinned_env):
        probe_facts.assert_c901_span(pinned_env / "bin" / "ruff")

    def test_disagreement_raises_tooling_error(self):
        # A ruff that reports a body-spanning C901 (or none at all) must
        # raise, not silently branch (distribution spec §6).
        with pytest.raises(ToolingError):
            probe_facts.assert_c901_span("/nonexistent/ruff-binary")

    def test_probe_fixture_trips_c901_on_pinned_ruff(self, pinned_env, tmp_path):
        src = tmp_path / "complex.py"
        src.write_text(probe_facts.C901_PROBE_SOURCE)
        proc = subprocess.run(
            [str(pinned_env / "bin" / "ruff"), "check", "--isolated",
             "--select", "C901", "--output-format", "json", str(src)],
            capture_output=True, text=True,
        )
        codes = [m.get("code") for m in json.loads(proc.stdout)]
        assert "C901" in codes


class TestMutmutVocabularyProbe:
    def test_recorded_vocabulary_is_complete(self):
        # addendum §4 outcome table — every state the adapter normalises
        for state in ("killed", "timeout", "survived", "no_tests", "suspicious",
                      "skipped", "caught_by_type_check", "segfault",
                      "not_checked", "interrupted"):
            assert state in probe_facts.MUTMUT_OUTCOME_STATES

    def test_probe_verified_versions_recorded(self):
        assert probe_facts.C901_PROBE_VERIFIED
        assert probe_facts.MUTMUT_PROBE_VERIFIED
