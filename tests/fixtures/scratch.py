"""Scratch repository generator — the unit of every self-test.

A scratch repo is a real git repository with:

  * a clean initial commit on ``main`` (application code + tests +
    .quality/ configuration rendered from the runner's templates),
  * optional follow-up commits or working-tree edits applying a
    violation,
  * the real CLI invoked against it.

Base counts are computed from the merge base at gate time (v5.1 §4.3),
so the initial commit *is* the ratchet reference.
"""

from __future__ import annotations

import shutil
import textwrap
from pathlib import Path

from ..conftest import TEMPLATES, run_git

# The analyzer pins used by scratch repositories. Byte-identical to the
# repo's .quality/toolchain.lock (pinned by a regression test): the
# analyzer-env cache key is sha256 of the lock bytes, so byte identity
# makes every scratch run share the repo's analyzer environment — one
# env, one cache entry, no second cold build per suite run.
SCRATCH_TOOLCHAIN = """\
# Analyzer pins (v5.1 §4.4). Protected path (§11.2): an agent
# that can bump Ruff can shift every ratchet reference without
# touching a threshold. Exact versions, never ranges.
schema_version = 1
runner_version = "0.2.0"
spec_version = "v5.1"
addendum_version = "final"

[tools]
ruff = "0.16.3"
pyright = "1.1.411"
pytest = "9.1.1"
pytest-cov = "7.0.0"
semgrep = "1.173.0"
xenon = "0.9.3"
deptry = "0.25.1"
pip-audit = "2.10.1"
pyscn = "1.29.1"
"""

SCRATCH_PYPROJECT = """\
[project]
name = "scratch"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = []
"""

CLEAN_APP = """\
\"\"\"A deliberately small, clean application module.\"\"\"


def add(a: int, b: int) -> int:
    return a + b


def greet(name: str) -> str:
    return f"hello {name}"
"""

CLEAN_TEST = """\
from scratch.app import add, greet


def test_add():
    assert add(2, 3) == 5


def test_greet():
    assert greet("ada") == "hello ada"
"""


def write_files(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(content), encoding="utf-8")


def install_quality(root: Path, *, base_ref: str = "main") -> None:
    """Render .quality/ + dotfiles from the runner's templates."""
    quality = root / ".quality"
    quality.mkdir(parents=True, exist_ok=True)
    shutil.copy(TEMPLATES / "quality" / "ruff.toml", quality / "ruff.toml")
    shutil.copytree(TEMPLATES / "quality" / "semgrep", quality / "semgrep")
    shutil.copy(TEMPLATES / "quality" / "pytest.ini", quality / "pytest.ini")
    (root / "pyrightconfig.json").write_text(
        (TEMPLATES / "pyrightconfig.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    config = (TEMPLATES / "quality" / "config.toml").read_text(encoding="utf-8")
    config = config.replace('ref = "main"', f'ref = "{base_ref}"')
    (quality / "config.toml").write_text(config, encoding="utf-8")
    # Bytes, not text: a text-mode write translates \n to the platform
    # line ending on Windows, and the env cache key is the lock's bytes.
    (quality / "toolchain.lock").write_bytes(SCRATCH_TOOLCHAIN.encode("utf-8"))


def make_repo(
    path: Path,
    *,
    app: str = CLEAN_APP,
    test: str = CLEAN_TEST,
    with_quality: bool = True,
    with_tests: bool = True,
    base_branch: str = "main",
    extra_files: dict[str, str] | None = None,
) -> Path:
    """Create a scratch repo with one clean commit on *base_branch*."""
    path.mkdir(parents=True, exist_ok=True)
    run_git("init", "-b", base_branch, cwd=path)
    files = {
        "pyproject.toml": SCRATCH_PYPROJECT,
        "src/scratch/__init__.py": "",
        "src/scratch/app.py": app,
    }
    if with_tests:
        files["tests/test_app.py"] = test
    if extra_files:
        files.update(extra_files)
    write_files(path, files)
    if with_quality:
        install_quality(path, base_ref=base_branch)
    run_git("add", "-A", cwd=path)
    run_git("commit", "-q", "-m", "clean scratch baseline", cwd=path)
    return path


def commit(repo: Path, files: dict[str, str], message: str, *, branch: str | None = None) -> None:
    """Write files and commit them (optionally on a new branch first)."""
    if branch:
        run_git("checkout", "-q", "-b", branch, cwd=repo)
    write_files(repo, files)
    run_git("add", "-A", cwd=repo)
    run_git("commit", "-q", "-m", message, cwd=repo)


def edit(repo: Path, files: dict[str, str]) -> None:
    """Write files as uncommitted working-tree edits."""
    write_files(repo, files)
