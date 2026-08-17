"""Regression tests for the adversarial-verification findings
(spec-verification workflow, confirmed items).

Each test names the confirmed finding it pins down. The critical one:
deleting a protected path was invisible to the integrity gate because
the -U0 hunk parser drops the ``+++ /dev/null`` side of deletions — an
implementation agent could delete AGENTS.md, .quality/allowlist.toml
or the whole semgrep ruleset and no gate would say a word.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from tests.conftest import run_git
from tests.fixtures.scratch import commit, make_repo
from tests.test_selftests import run_full


AGENTS_MD = "# Conventions\n\nBe excellent to each other.\n"


class TestProtectedPathDeletionAndRename:
    def test_delete_agents_md_fires_integrity(self, tmp_path):
        repo = make_repo(tmp_path / "del-agents", extra_files={"AGENTS.md": AGENTS_MD})
        run_git("checkout", "-q", "-b", "feature", cwd=repo)
        run_git("rm", "-q", "AGENTS.md", cwd=repo)
        run_git("commit", "-q", "-m", "delete AGENTS.md", cwd=repo)
        code, report = run_full(repo)
        gate = report["gates"]["integrity"]
        assert gate["status"] == "fail", gate
        assert "AGENTS.md" in gate["detail"]
        assert code == 1

    def test_delete_allowlist_fires_integrity(self, tmp_path):
        repo = make_repo(tmp_path / "del-allow")
        (repo / ".quality" / "allowlist.toml").write_text("", encoding="utf-8")
        run_git("add", "-A", cwd=repo)
        run_git("commit", "-q", "-m", "add empty allowlist", cwd=repo)
        run_git("checkout", "-q", "-b", "feature", cwd=repo)
        run_git("rm", "-q", ".quality/allowlist.toml", cwd=repo)
        run_git("commit", "-q", "-m", "remove allowlist", cwd=repo)
        code, report = run_full(repo)
        gate = report["gates"]["integrity"]
        assert gate["status"] == "fail", gate
        assert ".quality/allowlist.toml" in gate["detail"]

    def test_rename_protected_path_fires_integrity(self, tmp_path):
        # --no-renames makes a 100%-similarity rename a delete+add of
        # the protected path: the deletion side must stay visible.
        repo = make_repo(tmp_path / "rename-agents", extra_files={"AGENTS.md": AGENTS_MD})
        run_git("checkout", "-q", "-b", "feature", cwd=repo)
        run_git("mv", "AGENTS.md", "AGENTS-renamed.md", cwd=repo)
        run_git("commit", "-q", "-m", "rename AGENTS.md", cwd=repo)
        code, report = run_full(repo)
        gate = report["gates"]["integrity"]
        assert gate["status"] == "fail", gate
        assert "AGENTS.md" in gate["detail"]

    def test_rename_violating_module_still_diff_scoped(self, tmp_path):
        # The other half: renaming a module must not let its findings
        # slip past the diff scope.
        repo = make_repo(tmp_path / "rename-mod")
        src = "src/scratch/evil.py"
        (repo / src).write_text("def compute(expr: str) -> int:\n    return eval(expr)\n", encoding="utf-8")
        run_git("add", "-A", cwd=repo)
        run_git("commit", "-q", "-m", "add evil module", cwd=repo)
        run_git("checkout", "-q", "-b", "feature", cwd=repo)
        run_git("mv", src, "src/scratch/renamed.py", cwd=repo)
        run_git("commit", "-q", "-m", "rename module", cwd=repo)
        code, report = run_full(repo)
        assert code == 1
        assert report["gates"]["ruff-s"]["status"] == "fail"


def commit_remaining(repo):  # retained for symmetry with deletion tests
    run_git("commit", "-q", "-m", "deletion", cwd=repo)


class TestSpecVersionInToolchainLock:
    def test_lock_records_spec_and_addendum_version(self, tmp_path):
        from aufsicht import ADDENDUM_VERSION, SPEC_VERSION

        repo = make_repo(tmp_path / "specver")
        text = (repo / ".quality" / "toolchain.lock").read_text(encoding="utf-8")
        assert f'spec_version = "{SPEC_VERSION}"' in text
        assert f'addendum_version = "{ADDENDUM_VERSION}"' in text

    def test_loader_parses_spec_version(self):
        from aufsicht import SPEC_VERSION
        from aufsicht.init.pins import render_toolchain_lock
        from aufsicht.toolchain import Toolchain, load_toolchain

        rendered = render_toolchain_lock("9.9.9")
        import pathlib, tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / ".quality").mkdir()
            (root / ".quality" / "toolchain.lock").write_text(rendered)
            lock = load_toolchain(root)
            assert lock.spec_version == SPEC_VERSION
            assert lock.runner_version == "9.9.9"
            assert "pytest-cov" in lock.tools

    def test_runner_selftest_version_assertions_cover_spec(self, tmp_path):
        from aufsicht import SPEC_VERSION

        repo = make_repo(tmp_path / "specver-report")
        code, report = run_full(repo)
        assert report["spec_version"] == SPEC_VERSION


class TestBranchCoverageTelemetry:
    def test_full_run_records_coverage_metric(self, tmp_path):
        # v5.1 §19: "pytest with branch coverage in the gate" — recorded
        # as telemetry (§8: never gated at Tier 1).
        repo = make_repo(tmp_path / "cov")
        code, report = run_full(repo)
        assert code == 0, json.dumps(report, indent=2)
        pytest_gate = report["gates"]["pytest"]
        assert "branch_coverage_percent" in pytest_gate, pytest_gate
        assert isinstance(pytest_gate["branch_coverage_percent"], (int, float))
        assert report["metrics"]["coverage"]["head"] >= 0


class TestModelASignatureVerification:
    """v5.1 §11.1 model A: signed commits verified against an
    allowed-signers file — with the real ssh-keygen -Y verify, over the
    real signature blob and payload."""

    def _signed_repo(self, tmp_path, *, sign: bool):
        repo = make_repo(tmp_path / ("signed" if sign else "unsigned"))
        # SSH signing key + allowed-signers file, committed at BASE so
        # the signers file itself is not a protected-path change later.
        key = tmp_path / "key"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
            check=True, capture_output=True,
        )
        principal = "tester@scratch"
        signers = repo / ".quality" / "allowed_signers"
        signers.write_text(
            f"{principal} {key.with_suffix('.pub').read_text().strip()}\n",
            encoding="utf-8",
        )
        cfg = (repo / ".quality" / "config.toml").read_text(encoding="utf-8")
        cfg = cfg.replace('deployment_model = "B"', 'deployment_model = "A"')
        (repo / ".quality" / "config.toml").write_text(cfg, encoding="utf-8")
        run_git("add", "-A", cwd=repo)
        run_git("commit", "-q", "-m", "configure model A", cwd=repo)

        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Scratch Tester",
            "GIT_AUTHOR_EMAIL": "scratch@example.invalid",
            "GIT_COMMITTER_NAME": "Scratch Tester",
            "GIT_COMMITTER_EMAIL": "scratch@example.invalid",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        }
        def gitcfg(*pairs):
            run_git(*pairs, cwd=repo)
        gitcfg("config", "gpg.format", "ssh")
        gitcfg("config", "user.signingkey", str(key))
        if sign:
            gitcfg("config", "commit.gpgsign", "true")
        # A protected-path change on a branch.
        (repo / ".quality" / "config.toml").write_text(
            cfg.replace("c901_max = 10", "c901_max = 11"), encoding="utf-8"
        )
        run_git("checkout", "-q", "-b", "feature", cwd=repo)
        run_git("add", "-A", cwd=repo)
        subprocess.run(
            ["git", "commit", "-q", "-m", "guardrail change"],
            cwd=str(repo), check=True, capture_output=True, env=env,
        )
        return repo

    def test_validly_signed_protected_change_passes_integrity(self, tmp_path):
        repo = self._signed_repo(tmp_path, sign=True)
        code, report = run_full(repo)
        gate = report["gates"]["integrity"]
        assert gate["status"] == "pass", gate

    def test_unsigned_protected_change_fails_model_a(self, tmp_path):
        repo = self._signed_repo(tmp_path, sign=False)
        code, report = run_full(repo)
        gate = report["gates"]["integrity"]
        assert gate["status"] == "fail", gate
        assert "not SSH-signed" in gate["detail"] or "did not verify" in gate["detail"]

    def test_signature_by_wrong_key_fails(self, tmp_path):
        repo = self._signed_repo(tmp_path, sign=True)
        # Swap the allowed-signers principal to a DIFFERENT key.
        other = tmp_path / "other"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other)],
            check=True, capture_output=True,
        )
        (repo / ".quality" / "allowed_signers").write_text(
            "tester@scratch " + other.with_suffix(".pub").read_text().strip() + "\n",
            encoding="utf-8",
        )
        run_git("add", "-A", cwd=repo)
        run_git("commit", "-q", "-m", "wrong signer", cwd=repo)
        code, report = run_full(repo)
        gate = report["gates"]["integrity"]
        assert gate["status"] == "fail", gate
        assert "did not verify" in gate["detail"]


class TestExemptionRowStrengthening:
    def test_lockfile_change_leaves_pyscn_ratcheting(self, tmp_path):
        # §18: "project lockfile changes → Pyright/deptry exempt ...
        # Ruff/pyscn still ratchet" — assert the pyscn half explicitly.
        repo = make_repo(tmp_path / "pyscn-still")
        pyproject = (repo / "pyproject.toml").read_text(encoding="utf-8")
        pyproject += '\n[project.optional-dependencies]\nextra = ["click"]\n'
        commit(repo, {"pyproject.toml": pyproject}, "add optional dep", branch="feature")
        code, report = run_full(repo)
        assert "pyscn" not in report["exempt_tools"], report["exempt_tools"]
        cycles = report["gates"]["cycles"]
        deadcode = report["gates"]["deadcode"]
        # Not exempt means the ratchet ran (pass or fail on merits, but
        # never 'exempt').
        assert cycles["status"] != "exempt"
        assert deadcode["status"] != "exempt"
