"""`aufsicht init` self-tests (distribution spec §8 init-specific
cases + §5 contract). Each case is a scratch repository and the real
CLI invoked.
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import run_cli, run_git
from tests.fixtures.scratch import make_repo, write_files


def branch_of(repo) -> str:
    return run_git("symbolic-ref", "--short", "HEAD", cwd=repo).stdout.strip()


def status_clean(repo) -> bool:
    return run_git("status", "--porcelain", cwd=repo).stdout.strip() == ""


class TestDryRun:
    def test_non_tty_defaults_to_dry_run(self, tmp_path):
        # run_cli captures stdout → not a TTY → plan, not a mutation.
        repo = make_repo(tmp_path / "plan", with_quality=False)
        proc = run_cli("init", "--json", cwd=repo)
        assert proc.returncode == 0, proc.stderr
        plan = json.loads(proc.stdout)
        assert plan["dry_run"] is True
        assert status_clean(repo)
        assert not (repo / ".quality").exists()
        assert branch_of(repo) == "main"

    def test_plan_names_counts_before_writing(self, tmp_path):
        repo = make_repo(tmp_path / "counts", with_quality=False)
        proc = run_cli("init", "--json", cwd=repo)
        plan = json.loads(proc.stdout)
        assert "day_one_allowlist" in plan
        assert "cycles" in plan["day_one_allowlist"]
        assert "probes" in plan and plan["probes"]["decisions"]


class TestRefusals:
    def test_run_twice_second_exits_1_changes_nothing(self, tmp_path):
        repo = make_repo(tmp_path / "twice", with_quality=False)
        first = run_cli("init", "--write", "--json", cwd=repo)
        assert first.returncode in (0, 2), first.stderr
        head_after_first = run_git("rev-parse", "HEAD", cwd=repo).stdout
        second = run_cli("init", "--write", cwd=repo)
        assert second.returncode == 1
        assert "already present" in second.stderr
        assert run_git("rev-parse", "HEAD", cwd=repo).stdout == head_after_first
        assert status_clean(repo)

    def test_force_on_dirty_tree_refuses(self, tmp_path):
        repo = make_repo(tmp_path / "dirty", with_quality=False)
        first = run_cli("init", "--write", "--json", cwd=repo)
        assert first.returncode in (0, 2), first.stderr
        (repo / "src" / "scratch" / "app.py").write_text("x = 1\n", encoding="utf-8")
        proc = run_cli("init", "--write", "--force", cwd=repo)
        assert proc.returncode == 1
        assert "dirty" in proc.stderr

    def test_force_on_clean_tree_reinstalls(self, tmp_path):
        repo = make_repo(tmp_path / "reforce", with_quality=False)
        assert run_cli("init", "--write", cwd=repo).returncode in (0, 2)
        proc = run_cli("init", "--write", "--force", cwd=repo)
        assert proc.returncode in (0, 2), proc.stderr

    def test_shallow_clone_refuses_with_fetch_depth_remedy(self, tmp_path):
        repo = make_repo(tmp_path / "shallow", with_quality=False)
        (repo / ".git/shallow").write_text(
            run_git("rev-parse", "HEAD", cwd=repo).stdout.strip(), encoding="utf-8"
        )
        proc = run_cli("init", "--write", cwd=repo)
        assert proc.returncode == 1
        assert "fetch-depth" in proc.stderr or "unshallow" in proc.stderr

    def test_ruff_config_in_pyproject_refuses_and_names_sections(self, tmp_path):
        repo = make_repo(tmp_path / "pyproject-collision", with_quality=False)
        # Put ruff config INTO pyproject (the §5.4 collision).
        pyproject = (repo / "pyproject.toml").read_text(encoding="utf-8")
        pyproject += '\n[tool.ruff]\nline-length = 88\n'
        (repo / "pyproject.toml").write_text(pyproject, encoding="utf-8")
        run_git("add", "-A", cwd=repo)
        run_git("commit", "-q", "-m", "add ruff config to pyproject", cwd=repo)
        proc = run_cli("init", "--write", cwd=repo)
        assert proc.returncode == 1
        assert "[tool.ruff]" in proc.stderr
        assert not (repo / ".quality").exists()

    def test_existing_aufsicht_workflow_refuses_no_merge(self, tmp_path):
        repo = make_repo(
            tmp_path / "wf", with_quality=False,
            extra_files={".github/workflows/aufsicht.yml": "name: existing\n"},
        )
        proc = run_cli("init", "--write", cwd=repo)
        assert proc.returncode == 1
        assert "aufsicht.yml" in proc.stderr
        assert (repo / ".github/workflows/aufsicht.yml").read_text() == "name: existing\n"

    def test_not_a_git_repository(self, tmp_path):
        bare = tmp_path / "notgit"
        bare.mkdir()
        proc = run_cli("init", "--write", cwd=bare)
        assert proc.returncode == 1
        assert "not a git repository" in proc.stderr


class TestWritePhase:
    def test_install_writes_branch_not_default(self, tmp_path):
        repo = make_repo(tmp_path / "install", with_quality=False)
        proc = run_cli("init", "--write", "--json", cwd=repo)
        assert proc.returncode in (0, 2), proc.stderr
        assert branch_of(repo).startswith("aufsicht/init")
        assert branch_of(repo) != "main"
        for rel in (
            ".quality/config.toml", ".quality/ruff.toml", ".quality/pytest.ini",
            ".quality/toolchain.lock", "pyrightconfig.json",
            ".quality/semgrep/test-disabling.yaml", "AGENTS.md",
        ):
            assert (repo / rel).is_file(), f"missing {rel}"
        assert status_clean(repo)  # everything committed

    def test_project_dependency_graph_unchanged(self, tmp_path):
        # dist §8: "runner absent from project deps → assert project
        # lockfile unchanged by install"
        repo = make_repo(tmp_path / "deps", with_quality=False)
        run_git("checkout", "-q", "-b", "tmp", cwd=repo)
        import subprocess

        subprocess.run(["uv", "lock", "-q"], cwd=str(repo), check=True,
                       capture_output=True)
        run_git("add", "-A", cwd=repo)
        run_git("commit", "-q", "-m", "add lockfile", cwd=repo)
        run_git("checkout", "-q", "main", cwd=repo)
        run_git("merge", "-q", "--ff-only", "tmp", cwd=repo)
        pyproject_before = (repo / "pyproject.toml").read_bytes()
        lock_before = (repo / "uv.lock").read_bytes()
        assert run_cli("init", "--write", cwd=repo).returncode in (0, 2)
        assert (repo / "pyproject.toml").read_bytes() == pyproject_before
        assert (repo / "uv.lock").read_bytes() == lock_before
        assert "aufsicht" not in pyproject_before.decode()

    def test_no_tests_repo_installs_and_reports_honestly(self, tmp_path):
        repo = make_repo(tmp_path / "notests", with_quality=False, with_tests=False)
        proc = run_cli("init", "--write", "--json", cwd=repo)
        assert proc.returncode in (0, 2), proc.stderr
        gate = run_cli("full", cwd=repo)
        report = json.loads(gate.stdout)
        assert report["gates"]["pytest"]["status"] in ("skipped", "pass"), report["gates"]["pytest"]
        assert "no tests" in (report["gates"]["pytest"].get("detail") or "")

    def test_existing_agents_md_appended_not_rewritten(self, tmp_path):
        existing = "# Project conventions\n\nWe use src layout.\n"
        repo = make_repo(
            tmp_path / "agents", with_quality=False,
            extra_files={"AGENTS.md": existing},
        )
        assert run_cli("init", "--write", cwd=repo).returncode in (0, 2)
        content = (repo / "AGENTS.md").read_text(encoding="utf-8")
        assert content.startswith(existing.rstrip("\n"))
        assert content.count("aufsicht:begin") == 1
        assert "Never, under any circumstances" in content

        # --force replaces only between the delimiters.
        section_start = content.index("<!-- aufsicht:begin")
        between = content[section_start:]
        mutated = content[:section_start] + between.replace(
            "quality-fast", "quality-fast (edited)"
        )
        (repo / "AGENTS.md").write_text(mutated, encoding="utf-8")
        run_git("add", "-A", cwd=repo)
        run_git("commit", "-q", "-m", "edit section", cwd=repo)
        assert run_cli("init", "--write", "--force", cwd=repo).returncode in (0, 2)
        after = (repo / "AGENTS.md").read_text(encoding="utf-8")
        assert after.startswith(existing.rstrip("\n"))
        assert "(edited)" not in after  # section rewritten between delimiters
        assert after.count("Aufsicht") + after.count("aufsicht") >= 1

    def test_root_ruff_config_absorbed(self, tmp_path):
        repo = make_repo(
            tmp_path / "absorb", with_quality=False,
            extra_files={"ruff.toml": 'line-length = 120\n\n[lint]\nselect = ["E", "F"]\n'},
        )
        assert run_cli("init", "--write", cwd=repo).returncode in (0, 2)
        assert not (repo / "ruff.toml").exists()
        moved = (repo / ".quality" / "ruff.toml").read_text(encoding="utf-8")
        assert "line-length = 120" in moved

    def test_ci_workflow_emitted_with_fetch_depth_zero(self, tmp_path):
        repo = make_repo(tmp_path / "ci", with_quality=False)
        assert run_cli("init", "--write", cwd=repo).returncode in (0, 2)
        wf = (repo / ".github" / "workflows" / "aufsicht.yml").read_text(encoding="utf-8")
        assert "fetch-depth: 0" in wf
        assert "aufsicht==" in wf


class TestVerifyPhase:
    def test_verify_runs_quality_full_and_expects_integrity(self, tmp_path):
        repo = make_repo(tmp_path / "verify", with_quality=False)
        proc = run_cli("init", "--write", "--json", cwd=repo)
        assert proc.returncode in (0, 2), proc.stderr
        out = json.loads(proc.stdout)
        verify = out["verify"]
        integrity = verify["gates"].get("integrity", {})
        # The installation PR changes protected paths: the integrity
        # failure IS the review surface (distribution §5.5, v5.1 §11.1).
        assert integrity.get("status") in ("fail", None)
        assert verify["expected_integrity_failure"]

    def test_installed_repo_gates_run(self, tmp_path):
        # After install (branch checked out), quality-full works against
        # the installed configuration.
        repo = make_repo(tmp_path / "gates", with_quality=False)
        assert run_cli("init", "--write", cwd=repo).returncode in (0, 2)
        proc = run_cli("full", cwd=repo)
        report = json.loads(proc.stdout)
        assert report["runner_version"]
        assert report["spec_version"] == "v5.1"
        assert report["gates"]["ruff"]["status"] == "pass"
