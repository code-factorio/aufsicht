"""v5.1 §18 ratchet and exemption self-tests.

The per-rule property is the one a plausible-looking wrong
implementation gets wrong: fixing one F401 while introducing one B006
nets to zero and a total-count ratchet passes it. Grouping by rule
costs one groupby and closes the case.
"""

from __future__ import annotations

import json

from tests.conftest import run_cli
from tests.fixtures.scratch import SCRATCH_TOOLCHAIN, commit, make_repo
from tests.test_selftests import run_full


class TestPerRuleRatchet:
    def test_fix_one_f401_add_one_b006(self, tmp_path):
        # §18: fix one F401, add one B006 → Ruff, per-rule ratchet
        # (a total ratchet passes this)
        base_app = "import os\n\n\ndef add(a: int, b: int) -> int:\n    return a + b\n"
        repo = make_repo(
            tmp_path / "swap",
            app=base_app,
            test=("from scratch.app import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"),
        )
        commit(
            repo,
            {
                # F401 fixed (import os removed), B006 introduced.
                # Same file → both diff-scoped and ratchet see it; the
                # assertion is that the ratchet reports the per-rule
                # regression while totals net to zero.
                "src/scratch/app.py": (
                    "def add(a: int, b: int, acc: list = []) -> int:\n    return a + b + len(acc)\n"
                ),
            },
            "fix F401, introduce B006",
            branch="feature",
        )
        code, report = run_full(repo)
        assert code == 1
        gate = report["gates"]["ruff"]
        assert gate["status"] == "fail"
        assert "per-rule-ratchet" in gate["mechanism"]
        ratchet = gate["ratchet"]
        regressed = {r["rule"]: r for r in ratchet["regressed_rules"]}
        assert "B006" in regressed, ratchet
        assert regressed["B006"]["base"] == 0
        assert regressed["B006"]["head"] == 1
        # The whole point: totals net out (a total ratchet would pass).
        assert ratchet["totals"]["base"] == ratchet["totals"]["head"], ratchet

    def test_same_rule_swap_still_passes_tier1(self, tmp_path):
        # v5.1 §4.3 residual gameability, accepted for Tier 1: two
        # instances of the same rule in different files swap cleanly.
        # This documents the accepted limit, not a bug.
        base_app = "import os\n\n\ndef add(a: int, b: int) -> int:\n    return a + b\n"
        repo = make_repo(tmp_path / "swap2", app=base_app)
        commit(
            repo,
            {
                "src/scratch/app.py": (
                    "import os\n"
                    "import json\n"
                    "\n"
                    "\n"
                    "def add(a: int, b: int) -> int:\n"
                    "    return a + b\n"
                ),
            },
            "swap F401s",
            branch="feature",
        )
        _code, report = run_full(repo)
        # F401: base 1 → head 2 would regress... but diff-scoped flags
        # the new F401 in the changed file anyway. The ratchet's totals
        # rose by one, so this fails via both — the interesting case is
        # fix-one-add-one of the same rule, which nets to zero:
        commit(
            repo,
            {
                "src/scratch/app.py": (
                    "import json\n\n\ndef add(a: int, b: int) -> int:\n    return a + b\n"
                ),
            },
            "swap same rule: fix os, add json",
        )
        _code, report = run_full(repo)
        gate = report["gates"]["ruff"]
        assert gate["ratchet"]["regressed_rules"] == []
        assert gate["ratchet"]["totals"]["base"] == gate["ratchet"]["totals"]["head"]


class TestExemptions:
    def _repo(self, tmp_path, name):
        return make_repo(tmp_path / name)

    def test_config_change_exempts_only_that_tool(self, tmp_path):
        # §18: approved PR enabling a new rule → that tool exempt;
        # other tools still ratchet. (The integrity gate also fires on
        # the protected-path edit — correctly, since it needs review.)
        repo = self._repo(tmp_path, "cfg-change")
        ruff_toml = (repo / ".quality" / "ruff.toml").read_text(encoding="utf-8")
        ruff_toml = ruff_toml.replace(
            '    "C901", # per-function complexity (changed-file scope, v5.1 §4.2)',
            '    "C901", # per-function complexity (changed-file scope, v5.1 §4.2)\n    "C4",',
        )
        commit(repo, {".quality/ruff.toml": ruff_toml}, "enable C4 rules", branch="feature")
        _code, report = run_full(repo)
        assert report["exempt_tools"] == ["ruff"], report["exempt_tools"]
        assert "ruff" in report["exemption_reasons"]
        assert report["gates"]["ruff"]["ratchet"] == "exempt"
        # The integrity tripwire fires on the protected-path edit.
        assert report["gates"]["integrity"]["status"] == "fail"

    def test_toolchain_bump_exempts_that_tool_only(self, tmp_path):
        # §18: toolchain.lock bumps Ruff → Ruff exempt, Pyright/deptry
        # still ratchet.
        repo = self._repo(tmp_path, "toolchain-bump")
        bumped = SCRATCH_TOOLCHAIN.replace('ruff = "0.16.3"', 'ruff = "0.16.2"')
        commit(repo, {".quality/toolchain.lock": bumped}, "bump ruff pin", branch="feature")
        _code, report = run_full(repo)
        assert report["exempt_tools"] == ["ruff"], report["exempt_tools"]
        assert "toolchain.lock pin for ruff changed" in report["exemption_reasons"]["ruff"]

    def test_project_lockfile_change_exemptions(self, tmp_path):
        # §18: project lockfile changes → Pyright/deptry exempt, flagged
        # in report; Ruff/pyscn still ratchet.
        repo = self._repo(tmp_path, "lockfile")
        pyproject = (repo / "pyproject.toml").read_text(encoding="utf-8")
        pyproject += '\n[project.optional-dependencies]\nextra = ["click"]\n'
        commit(repo, {"pyproject.toml": pyproject}, "add optional dep", branch="feature")
        _code, report = run_full(repo)
        assert report["dependency_environment_changed"] is True
        assert sorted(report["exempt_tools"]) == ["deptry", "pyright"], report["exempt_tools"]
        assert "ruff" not in report["exempt_tools"]

    def test_runner_version_change_exempts_all_ratchets(self, tmp_path):
        # distribution spec §10: a runner upgrade is a toolchain bump —
        # exempt the affected analyzers for that PR and say so.
        repo = self._repo(tmp_path, "runner-bump")
        bumped = SCRATCH_TOOLCHAIN.replace('runner_version = "0.2.0"', 'runner_version = "0.3.0"')
        commit(repo, {".quality/toolchain.lock": bumped}, "upgrade runner", branch="feature")
        _code, report = run_full(repo)
        assert "ruff" in report["exempt_tools"]
        assert "runner version changed" in report["exemption_reasons"]["ruff"]

    def test_base_run_uses_base_lockfile_not_head_deps(self, tmp_path):
        # §18: BASE analysed with HEAD deps must NOT happen.
        from aufsicht import ratchet as ratchet_mod
        from aufsicht.toolchain import project_env_key

        repo = self._repo(tmp_path, "env-split")
        pyproject = (repo / "pyproject.toml").read_text(encoding="utf-8")
        pyproject += '\n[project.optional-dependencies]\nextra = ["click"]\n'
        commit(repo, {"pyproject.toml": pyproject}, "add optional dep", branch="feature")

        base_wt = ratchet_mod.base_worktree(repo, repo_sha(repo), None)
        base_key, _ = project_env_key(base_wt)
        head_key, _ = project_env_key(repo)
        assert base_key != head_key, (
            "BASE and HEAD dependency environments must key differently "
            "when the lockfile differs (v5.1 §4.4)"
        )


def repo_sha(repo) -> str:
    from tests.conftest import run_git

    return run_git("rev-parse", "main", cwd=repo).stdout.strip()


class TestFailClosed:
    def test_shallow_clone_in_ci_exits_3(self, tmp_path):
        repo = make_repo(tmp_path / "shallow")
        from tests.conftest import run_git

        (repo / ".git/shallow").write_text(
            run_git("rev-parse", "HEAD", cwd=repo).stdout.strip(), encoding="utf-8"
        )
        proc = run_cli("full", cwd=repo)
        assert proc.returncode == 3
        report = json.loads(proc.stdout)
        assert report["status"] == "tooling-error"
        assert "fetch-depth" in report["tooling_error"]["remedy"]

    def test_unresolvable_base_exits_3(self, tmp_path):
        # No CI vars, no [base] ref, no remote — fail closed, never a
        # silent pass.
        repo = make_repo(tmp_path / "nobase", with_quality=False)
        proc = run_cli("full", cwd=repo)
        assert proc.returncode == 3
        report = json.loads(proc.stdout)
        assert report["status"] == "tooling-error"
        assert report["tooling_error"]["message"]


class TestBaseWorktreeLocking:
    def test_concurrent_same_sha_creation_is_safe(self, tmp_path):
        # AUFSI-mk90zn: xdist workers whose scratch repos share a
        # deterministic initial commit race on one base SHA. The
        # SHA-keyed lock must let every caller through; the unguarded
        # check-then-create lost the race with "already exists" (exit 3).
        from concurrent.futures import ThreadPoolExecutor

        from aufsicht import ratchet as ratchet_mod

        repo = make_repo(tmp_path / "repo")
        sha = repo_sha(repo)
        cache = tmp_path / "cache"
        with ThreadPoolExecutor(max_workers=8) as pool:
            paths = list(
                pool.map(
                    lambda _: ratchet_mod.base_worktree(repo, sha, cache),
                    range(8),
                )
            )
        assert len({str(path) for path in paths}) == 1
        assert (paths[0] / ".git").exists()
        assert not (cache / "worktrees" / f"{sha}.lock").exists()

    def test_stale_lock_is_taken_over(self, tmp_path):
        # A lock left behind by a dead holder must not block creation
        # forever: past the staleness age AND with a dead recorded PID
        # the next caller takes it over.
        import os as os_mod

        from aufsicht import ratchet as ratchet_mod

        repo = make_repo(tmp_path / "repo")
        sha = repo_sha(repo)
        cache = tmp_path / "cache" / "worktrees"
        cache.mkdir(parents=True)
        lock = cache / f"{sha}.lock"
        lock.write_text("999999", encoding="utf-8")  # no such process
        os_mod.utime(lock, (0, 0))  # far past any staleness threshold
        wt = ratchet_mod.base_worktree(repo, sha, cache.parent)
        assert (wt / ".git").exists()

    def test_live_holder_is_not_stolen(self, tmp_path, monkeypatch):
        # Age alone must not trigger takeover: a paused or slow creator
        # still owns its lock. With a live recorded PID the caller waits
        # and fails closed (exit 3) rather than stealing it.
        import os as os_mod
        import time as time_mod

        from aufsicht import ratchet as ratchet_mod
        from aufsicht.errors import ToolingError

        repo = make_repo(tmp_path / "repo")
        sha = repo_sha(repo)
        cache = tmp_path / "cache" / "worktrees"
        cache.mkdir(parents=True)
        lock = cache / f"{sha}.lock"
        lock.write_text(str(os_mod.getpid()), encoding="utf-8")  # alive
        os_mod.utime(lock, (0, 0))  # ancient, but the holder lives
        monkeypatch.setattr(ratchet_mod, "_WORKTREE_WAIT_SECONDS", 0.5)
        monkeypatch.setattr(ratchet_mod, "_WORKTREE_POLL_SECONDS", 0.02)
        started = time_mod.monotonic()
        with pytest.raises(ToolingError, match="still held"):
            ratchet_mod.base_worktree(repo, sha, cache.parent)
        assert time_mod.monotonic() - started >= 0.4
        assert lock.exists()  # not stolen

    def test_waiter_waits_for_checkout_completion(self, tmp_path):
        # PR review P1: `git worktree add` creates the .git link before
        # the checkout finishes. A .git file with the lock still held
        # means creation is in flight — the waiter must block until the
        # lock is released, not return an incomplete tree.
        import os as os_mod
        import threading
        import time as time_mod

        from aufsicht import ratchet as ratchet_mod

        repo = make_repo(tmp_path / "repo")
        sha = repo_sha(repo)
        cache = tmp_path / "cache"
        wt = cache / "worktrees" / sha
        wt.mkdir(parents=True)
        (wt / ".git").write_text("gitdir: fake-in-flight-link\n", encoding="utf-8")
        lock = wt.parent / f"{sha}.lock"
        lock.write_text(str(os_mod.getpid()), encoding="utf-8")  # alive: no takeover

        def release():
            time_mod.sleep(0.3)
            lock.unlink()

        threading.Thread(target=release).start()
        started = time_mod.monotonic()
        result = ratchet_mod.base_worktree(repo, sha, cache)
        assert time_mod.monotonic() - started >= 0.25
        assert result == wt

    def test_release_lock_only_removes_own(self, tmp_path):
        # A creator whose abandoned lock was taken over must not unlink
        # its successor's lock on the way out.
        from aufsicht import ratchet as ratchet_mod

        lock = tmp_path / "x.lock"
        lock.write_text("4242", encoding="utf-8")
        ratchet_mod._release_lock(lock, "1111")  # not ours anymore
        assert lock.exists()
        ratchet_mod._release_lock(lock, "4242")  # still ours
        assert not lock.exists()
