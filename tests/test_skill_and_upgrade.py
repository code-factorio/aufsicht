"""Layer 3 and upgrade self-tests (distribution spec §8, §9, §10).

`skill/scripts/bootstrap.sh` must be byte-identical to
`install/bootstrap.sh` — hand-syncing two copies is the same drift
problem wearing a different hat. SKILL.md carries frontmatter and its
prose is limited to §9's permitted list: no thresholds, no rule lists,
no report fields, no ratchet explanations, no agent behavioural rules
(those live in the target repo's AGENTS.md, where they are reviewable).
"""

from __future__ import annotations

import re
import stat
from pathlib import Path

from tests.conftest import REPO_ROOT, run_cli
from tests.fixtures.scratch import make_repo

INSTALL_BOOTSTRAP = REPO_ROOT / "install" / "bootstrap.sh"
SKILL_BOOTSTRAP = REPO_ROOT / "skill" / "scripts" / "bootstrap.sh"
SKILL_MD = REPO_ROOT / "skill" / "SKILL.md"


class TestBootstrapIdentity:
    def test_byte_identical(self):
        assert INSTALL_BOOTSTRAP.read_bytes() == SKILL_BOOTSTRAP.read_bytes()

    def test_executable(self):
        for path in (INSTALL_BOOTSTRAP, SKILL_BOOTSTRAP):
            mode = path.stat().st_mode
            assert mode & stat.S_IXUSR, f"{path} is not executable"

    def test_installs_pinned_runner_and_hands_off_to_init(self):
        text = INSTALL_BOOTSTRAP.read_text(encoding="utf-8")
        m = re.search(r'AUFSICHT_VERSION="([^"]+)"', text)
        assert m, "bootstrap must pin AUFSICHT_VERSION"
        assert "aufsicht==" in text
        assert "aufsicht init" in text, "bootstrap must hand off to aufsicht init"
        assert "exec aufsicht init" in text
        # Thin, per distribution spec §9: the bootstrap never writes
        # guardrail configuration itself.
        assert not re.search(r"(mkdir|touch|cat\s*>|tee)\s.*\.quality", text), \
            "bootstrap must not write .quality/ itself"

    def test_pinned_version_matches_runner(self):
        from aufsicht import __version__

        text = INSTALL_BOOTSTRAP.read_text(encoding="utf-8")
        assert f'AUFSICHT_VERSION="{__version__}"' in text


class TestSkillMd:
    def test_frontmatter_present(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        front = text.split("---", 2)[1]
        assert re.search(r"^name:\s*\S+\s*$", front, re.M)
        assert "description:" in front

    def test_no_gate_knowledge_in_layer3(self):
        # distribution spec §2: could it become wrong when the runner
        # is upgraded? Then it does not belong in the skill.
        banned = [
            "ratchet", "threshold", "<no-rule>", "C901", "S307", "PGH",
            "per-rule", "exit code", "report field", "DEP0",
        ]
        for path in SKILL_MD.parent.rglob("*"):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for word in banned:
                assert word.lower() not in text.lower(), (
                    f"{word!r} found in {path} — policy has leaked into Layer 3"
                )


class TestUpgrade:
    def test_upgrade_prints_diff_and_writes_nothing(self, tmp_path):
        repo = make_repo(tmp_path / "up", with_quality=False)
        assert run_cli("init", "--write", cwd=repo).returncode in (0, 2)
        from tests.conftest import run_git

        # Simulate an older installed runner version.
        lock = repo / ".quality" / "toolchain.lock"
        lock.write_text(
            lock.read_text(encoding="utf-8").replace(
                'runner_version = "0.1.0"', 'runner_version = "0.0.9"'
            ),
            encoding="utf-8",
        )
        run_git("add", "-A", cwd=repo)
        run_git("commit", "-q", "-m", "pretend older runner", cwd=repo)
        head_before = run_git("rev-parse", "HEAD", cwd=repo).stdout

        proc = run_cli("upgrade", "--repo", str(repo), cwd=REPO_ROOT)
        assert proc.returncode == 0, proc.stderr
        assert "runner_version" in proc.stdout or "toolchain.lock" in proc.stdout
        assert run_git("status", "--porcelain", cwd=repo).stdout == ""  # upgrade wrote nothing
        assert run_git("rev-parse", "HEAD", cwd=repo).stdout == head_before

    def test_upgrade_json_writes_nothing_flag(self, tmp_path):
        repo = make_repo(tmp_path / "up2", with_quality=False)
        assert run_cli("init", "--write", cwd=repo).returncode in (0, 2)
        import json

        proc = run_cli("upgrade", "--json", "--repo", str(repo), cwd=REPO_ROOT)
        assert proc.returncode == 0
        payload = json.loads(proc.stdout)
        assert payload["writes_anything"] is False
        assert "toolchain bump" in payload["note"]

    def test_upgrade_refuses_without_installation(self, tmp_path):
        repo = make_repo(tmp_path / "up3", with_quality=False)
        proc = run_cli("upgrade", "--repo", str(repo), cwd=REPO_ROOT)
        assert proc.returncode == 3
        assert "no .quality/" in proc.stderr
