"""pip-audit adapter self-tests (v5.1 §3 table, §8, §10.1).

Covers the §18 row "vulnerable dependency pinned → pip-audit, absolute".

The parser tests run against the *measured* JSON of pinned pip-audit
2.10.1 (captured by hand during the empirical probe — see the adapter
docstring for the full list of measured facts), so normalisation is
pinned to the tool's real output shape, not to this module's beliefs.

The end-to-end tests build real scratch repositories and invoke the
real CLI (`python -m aufsicht full`) in a subprocess. The two
vulnerable-dependency tests are network-bound and slow (~1-2 min each;
the advisory service is queried per pin) — they live in
:class:`TestSlowVulnerableDependency` at the bottom of this file. Run
the fast set only with:

    uv run pytest tests/test_pip_audit_adapter.py -q -k "not Slow"
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from pathlib import Path

import pytest

from aufsicht import pipeline
from aufsicht.adapters import pip_audit as pa
from aufsicht.base import BaseRef
from aufsicht.config import QualityConfig
from aufsicht.errors import ToolingError
from aufsicht.model import DiffModel
from aufsicht.report import Report
from aufsicht.toolchain import load_toolchain
from tests.conftest import run_cli
from tests.fixtures.scratch import make_repo

# Captured from the pinned pip-audit 2.10.1 with the exact flags the
# adapter passes (`--format json --progress-spinner off --desc off
# --aliases on --no-deps --disable-pip`), on a freeze of
# requests==2.19.1 and its transitive pins. Trimmed to the packages
# that carry findings; byte-for-byte real otherwise — including the
# duplicate advisory entries (PYSEC-2024-60 twice under idna) that the
# parser must collapse.
MEASURED_VULNERABLE_JSON = """{
  "dependencies": [
    {"name": "certifi", "version": "2026.7.22", "vulns": []},
    {"name": "chardet", "version": "3.0.4", "vulns": []},
    {"name": "idna", "version": "2.7", "vulns": [
      {"id": "PYSEC-2024-60", "fix_versions": ["3.7"],
       "aliases": ["GHSA-jjg7-2v4v-x38h", "CVE-2024-3651"]},
      {"id": "PYSEC-2024-60", "fix_versions": ["3.7"],
       "aliases": ["GHSA-jjg7-2v4v-x38h", "CVE-2024-3651"]}
    ]},
    {"name": "requests", "version": "2.19.1", "vulns": [
      {"id": "PYSEC-2018-28", "fix_versions": ["2.20.0"],
       "aliases": ["CVE-2018-18074", "GHSA-x84v-xcm2-53pg"]},
      {"id": "PYSEC-2023-74", "fix_versions": ["2.31.0"],
       "aliases": ["GHSA-j8r2-6x86-q33q", "CVE-2023-32681"]}
    ]},
    {"name": "urllib3", "version": "1.23", "vulns": [
      {"id": "PYSEC-2019-133", "fix_versions": ["1.24.2"],
       "aliases": ["CVE-2019-11324", "GHSA-mh33-7rrq-662w"]}
    ]}
  ],
  "fixes": []
}"""

# Measured: an audit with nothing to report.
MEASURED_CLEAN_JSON = '{"dependencies": [], "fixes": []}'


# --------------------------------------------------------------------------
# Parser and pure helpers — fast, no network, no CLI.
# --------------------------------------------------------------------------


class TestParseAudit:
    def test_measured_shape_parses(self):
        vulns = pa.parse_audit(MEASURED_VULNERABLE_JSON)
        by_id = {v.id: v for v in vulns}
        assert set(by_id) == {
            "PYSEC-2024-60", "PYSEC-2018-28", "PYSEC-2023-74", "PYSEC-2019-133",
        }
        assert by_id["PYSEC-2018-28"].name == "requests"
        assert by_id["PYSEC-2018-28"].version == "2.19.1"
        assert by_id["PYSEC-2018-28"].fix_versions == ("2.20.0",)
        assert "CVE-2018-18074" in by_id["PYSEC-2018-28"].aliases

    def test_duplicate_advisories_are_collapsed(self):
        # Measured: the PyPI advisory feed lists the same id twice under
        # one package; one vulnerability must be one finding.
        vulns = pa.parse_audit(MEASURED_VULNERABLE_JSON)
        pairs = [(v.name, v.id) for v in vulns]
        assert len(pairs) == len(set(pairs))
        assert sum(1 for v in vulns if v.id == "PYSEC-2024-60") == 1

    def test_clean_audit_is_empty(self):
        assert pa.parse_audit(MEASURED_CLEAN_JSON) == []

    def test_missing_vulns_key_tolerated(self):
        # A dependency dict without "vulns" contributes nothing rather
        # than crashing the whole audit.
        assert pa.parse_audit('{"dependencies": [{"name": "x"}], "fixes": []}') == []

    def test_unparseable_report_is_a_tooling_error(self):
        # Measured: pip-audit crashes exit 1 with EMPTY stdout — the
        # same exit code as "vulnerabilities found". Unparseable means
        # inconclusive, i.e. ToolingError (exit 3), never a pass.
        with pytest.raises(ToolingError):
            pa.parse_audit("")

    def test_wrong_shape_is_a_tooling_error(self):
        with pytest.raises(ToolingError):
            pa.parse_audit("[1, 2, 3]")


class TestFindingsFrom:
    def test_finding_shape(self):
        findings = pa.findings_from(pa.parse_audit(MEASURED_VULNERABLE_JSON))
        f = next(f for f in findings if f.rule == "pip-audit/PYSEC-2018-28")
        assert f.path == "pyproject.toml"  # the pin that pulled it
        assert f.symbol == "requests==2.19.1"
        assert "PYSEC-2018-28" in f.message
        assert "2.20.0" in f.message       # fix versions are actionable
        assert "severity" in f.message     # none exists — said out loud


class TestPinnedLines:
    def test_only_exact_pins_survive(self):
        freeze = (
            "\x1b[1mrequests\x1b[0m==2.19.1\n"  # uv's bold escape, measured
            "# a comment\n"
            "-e /some/local/path\n"
            "direct @ file:///tmp/pkg.whl\n"
            "urllib3==1.23\n"
        )
        assert pa.pinned_lines(freeze) == ["requests==2.19.1", "urllib3==1.23"]

    def test_empty_freeze_is_empty(self):
        assert pa.pinned_lines("") == []


class TestAliasDrift:
    def test_allowlisted_id_still_reported_is_drift(self):
        vulns = pa.parse_audit(MEASURED_VULNERABLE_JSON)
        # The allowlist said PYSEC-2018-28; the advisory now surfaces as
        # CVE-2018-18074 (an alias) and still appears in findings: the
        # ignore path failed — must be surfaced, not silently passed.
        drift = pa.alias_drift({"PYSEC-2018-28"}, vulns)
        assert len(drift) == 1
        assert "PYSEC-2018-28" in drift[0]
        assert "requests" in drift[0]

    def test_allowlisted_alias_still_reported_is_drift(self):
        # Same failure through the alias channel: the allowlist was
        # keyed on CVE-2023-32681, the advisory survived, and the alias
        # still links it — surfaced, never silently passed.
        vulns = pa.parse_audit(MEASURED_VULNERABLE_JSON)
        drift = pa.alias_drift({"CVE-2023-32681"}, vulns)  # alias of PYSEC-2023-74
        assert any("PYSEC-2023-74" in d for d in drift)

    def test_suppressed_or_fixed_advisory_is_not_drift(self):
        # An ignored id absent from the survivors is either the ignore
        # working (pip-audit matches by id OR alias — measured) or an
        # upgrade fixing the advisory; neither is drift, and staleness
        # of entries is the integrity gate's expiry business (v5.1 §10).
        survivors = [
            v for v in pa.parse_audit(MEASURED_VULNERABLE_JSON)
            if v.id != "PYSEC-2023-74"
        ]
        assert pa.alias_drift({"PYSEC-2023-74", "CVE-2023-32681"}, survivors) == []


# --------------------------------------------------------------------------
# Gate branches that need no network — driven through the real gate
# function with a real scratch repo and monkeypatched heavy steps.
# --------------------------------------------------------------------------


def _stub_context(repo: Path, mode: str = "full") -> pipeline.GateContext:
    return pipeline.GateContext(
        repo=repo,
        config=QualityConfig.load(repo),
        lock=load_toolchain(repo),
        base=BaseRef(source="config", ref="main", sha="0" * 40),
        diff=DiffModel(),
        env=repo / ".nonexistent-analyzer-env",
        cache=repo.parent / "cache",
        report=Report(base=None, command=mode),
        mode=mode,
    )


class TestGateBranches:
    def test_fast_mode_skips(self, tmp_path):
        repo = make_repo(tmp_path / "fastmode")
        result = pa.pip_audit_gate(_stub_context(repo, mode="fast"))
        assert result.status == "skipped"
        assert result.mechanism == "absolute"
        assert "full-mode" in (result.detail or "")

    def test_disabled_in_config_skips(self, tmp_path):
        repo = make_repo(tmp_path / "disabled")
        ctx = _stub_context(repo)
        ctx = dataclasses.replace(
            ctx, config=dataclasses.replace(ctx.config, pip_audit_enabled=False)
        )
        result = pa.pip_audit_gate(ctx)
        assert result.status == "skipped"
        assert "[pip_audit]" in (result.detail or "")

    def test_empty_freeze_skips_gracefully(self, tmp_path, monkeypatch):
        # The task's "clean scratch, no deps" fast path: a freeze with
        # zero exact pins must skip with a detail, not error (and not
        # hit the network for an empty audit).
        repo = make_repo(tmp_path / "emptyfreeze")
        monkeypatch.setattr(pa, "project_env", lambda repo, lock: repo)
        monkeypatch.setattr(pa, "freeze_requirements", lambda env, dest: [])
        result = pa.pip_audit_gate(_stub_context(repo))
        assert result.status == "skipped"
        assert "no dependencies to audit" in (result.detail or "")


# --------------------------------------------------------------------------
# End-to-end: real CLI, real environments, real network.
# --------------------------------------------------------------------------


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


def pip_audit_ids(report: dict) -> list[str]:
    return sorted(
        {
            f["rule"]
            for f in report["findings"]
            if f["gate"] == "pip-audit"
        }
    )


class TestCleanScratch:
    def test_pip_audit_gate_passes(self, tmp_path):
        # Fast path of the §18 negative case: a clean scratch repo
        # resolves a small dependency set (the pinned pytest and its
        # dependencies, per the per-commit project env) and the audit
        # over it passes with the absolute mechanism.
        repo = make_repo(tmp_path / "clean")
        _, report = run_full(repo)
        entry = report["gates"]["pip-audit"]
        assert entry["status"] == "pass", entry
        assert entry["mechanism"] == "absolute"
        assert entry["audited_pins"] >= 1
        assert pip_audit_ids(report) == []


# The two tests below are SLOW (network-bound, ~1-2 min each). They pin
# a genuinely vulnerable dependency (requests 2.19.1, whose advisories
# are historical and stable) and run the real CLI against the real
# advisory service. Skip with -k "not Slow" while iterating.
VULNERABLE_PYPROJECT = """\
[project]
name = "scratch"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["requests==2.19.1"]
"""

# The dependency must be *used* or the deptry ratchet flags it as an
# unused dependency regression (DEP002, verified clean against pinned
# deptry 0.25.1 with this exact shape). The import stays lazy — and out
# of module scope under TYPE_CHECKING — because the pinned requests
# 2.19.1 pulls urllib3 1.23, whose vendored six predates the modern
# interpreters the project env may use, so a module-level runtime
# import breaks *test collection* in an unrelated gate. The audit's
# subject is the pin, not a live HTTP call.
VULNERABLE_APP = '''\
"""A deliberately small application module that uses its one dependency."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import requests


def add(a: int, b: int) -> int:
    return a + b


def greet(name: str) -> str:
    return f"hello {name}"


def fetch(url: str) -> "requests.Response":
    import requests

    return requests.get(url)
'''

VULNERABLE_TEST = '''\
from scratch.app import add, fetch, greet


def test_add():
    assert add(2, 3) == 5


def test_greet():
    assert greet("ada") == "hello ada"


def test_fetch_is_callable():
    assert callable(fetch)
'''


def vulnerable_repo(path: Path, extra_files: dict[str, str] | None = None) -> Path:
    files = {"pyproject.toml": VULNERABLE_PYPROJECT}
    if extra_files:
        files.update(extra_files)
    return make_repo(
        path,
        app=VULNERABLE_APP,
        test=VULNERABLE_TEST,
        extra_files=files,
    )


class TestSlowVulnerableDependency:
    def test_vulnerable_dependency_fails_absolute(self, tmp_path):
        # §18: "vulnerable dependency pinned → pip-audit, absolute".
        repo = vulnerable_repo(tmp_path / "vuln")
        code, report = run_full(repo)
        assert code == 1, json.dumps(report.get("tooling_error"), indent=2)
        assert report["status"] == "fail"
        entry = report["gates"]["pip-audit"]
        assert entry["status"] == "fail", entry
        assert entry["mechanism"] == "absolute"
        ids = pip_audit_ids(report)
        assert ids, "expected pip-audit findings"
        # One advisory with a real database id, reported against the
        # pin that pulled it.
        assert any(
            r.startswith("pip-audit/") and ("PYSEC-" in r or "GHSA-" in r or "CVE-" in r)
            for r in ids
        ), ids
        gate_findings = [f for f in report["findings"] if f["gate"] == "pip-audit"]
        assert {f["path"] for f in gate_findings} == {"pyproject.toml"}
        assert all(f["rule"].startswith("pip-audit/") for f in gate_findings)
        # Audit ALL severities: findings include advisories without any
        # CVSS score — no severity floor may drop them (v5.1 §8).
        assert len(gate_findings) == len({f["rule"] for f in gate_findings})

    def test_allowlisted_vulnerabilities_pass(self, tmp_path):
        # v5.1 §10.1: a dated, reasoned allowlist entry per reported id,
        # committed at BASE (the human-approved path — an agent cannot
        # add it inside the PR, §11), suppresses exactly those
        # advisories via --ignore-vuln and the gate passes.
        #
        # Deterministic by construction: the entries are computed from
        # this run's own unallowlisted report, never hardcoded.
        repo = vulnerable_repo(tmp_path / "allow-step1")
        code, report = run_full(repo)
        assert code == 1
        assert report["gates"]["pip-audit"]["status"] == "fail"
        ids = pip_audit_ids(report)
        assert ids

        today = dt.datetime.now(tz=dt.UTC).date()
        added_on = today.isoformat()
        expires = (today + dt.timedelta(days=90)).isoformat()
        entries = "\n".join(
            f'[[entry]]\n'
            f'rule = "{rule}"\n'
            f'reason = "scratch self-test: measured advisory on the pinned requests"\n'
            f'added_by = "human"\n'
            f'added_on = "{added_on}"\n'
            f'expires = "{expires}"\n'
            for rule in ids
        )
        listed = vulnerable_repo(
            tmp_path / "allow-step2",
            extra_files={".quality/allowlist.toml": entries},
        )
        code2, report2 = run_full(listed)
        assert code2 == 0, json.dumps(report2, indent=2)
        entry2 = report2["gates"]["pip-audit"]
        assert entry2["status"] == "pass", entry2
        assert entry2["mechanism"] == "absolute"
        # The ignore-flag path demonstrably carried every allowlisted
        # id (visible in the report, v5.1 §15) — and nothing drifted.
        assert entry2["ignored_vulns"] == [i[len("pip-audit/"):] for i in ids]
        assert "allowlist_alias_drift" not in entry2
        assert pip_audit_ids(report2) == []
