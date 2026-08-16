"""Detect phase (distribution spec §5.1): package manager, layout,
test runner, task runner, CI provider, existing tool config and where
it lives. Pure observation — no decisions here.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import RefusalError

SUPPORTED_PACKAGE_MANAGERS = ("uv", "pip")


@dataclass
class Detection:
    package_manager: str
    layout: str                 # "src" | "flat"
    has_tests: bool
    test_runner: str | None
    task_runner: str | None
    ci_provider: str | None
    existing_ruff_config: str | None   # path, or None
    existing_pyright_config: str | None
    existing_pytest_config: str | None
    default_branch: str | None
    notes: list[str] = field(default_factory=list)


def detect(repo: Path) -> Detection:
    package_manager = _package_manager(repo)
    layout = "src" if (repo / "src").is_dir() else "flat"
    has_tests = (repo / "tests").is_dir()
    test_runner = "pytest" if has_tests or (repo / "pytest.ini").exists() else None

    task_runner = None
    if (repo / "Makefile").is_file():
        task_runner = "make"
    elif (repo / "justfile").is_file() or (repo / "Justfile").is_file():
        task_runner = "just"
    elif (repo / "pyproject.toml").is_file() and "[tool.poe" in (repo / "pyproject.toml").read_text(encoding="utf-8", errors="replace"):
        task_runner = "poe"

    ci_provider = None
    if (repo / ".github" / "workflows").is_dir():
        ci_provider = "github-actions"
    elif (repo / ".gitlab-ci.yml").is_file():
        ci_provider = "gitlab"

    ruff_cfg = next(
        (p for p in (".ruff.toml", "ruff.toml") if (repo / p).is_file()), None
    )
    pyright_cfg = "pyrightconfig.json" if (repo / "pyrightconfig.json").is_file() else None
    pytest_cfg = "pytest.ini" if (repo / "pytest.ini").is_file() else None

    from .. import gitutil
    default_branch = gitutil.default_branch(repo)

    return Detection(
        package_manager=package_manager,
        layout=layout,
        has_tests=has_tests,
        test_runner=test_runner,
        task_runner=task_runner,
        ci_provider=ci_provider,
        existing_ruff_config=ruff_cfg,
        existing_pyright_config=pyright_cfg,
        existing_pytest_config=pytest_cfg,
        default_branch=default_branch,
    )


def _package_manager(repo: Path) -> str:
    if (repo / "uv.lock").is_file():
        return "uv"
    for marker in ("poetry.lock", "pdm.lock"):
        if (repo / marker).is_file():
            raise RefusalError(
                f"package manager lockfile {marker} is not supported",
                remedy=f"Supported: {', '.join(SUPPORTED_PACKAGE_MANAGERS)}. "
                       "Migrate the lockfile (e.g. `uv lock`) or manage "
                       "aufsicht by hand.",
            )
    if shutil.which("uv"):
        return "uv"
    if shutil.which("pip") or shutil.which("pip3"):
        return "pip"
    raise RefusalError(
        "no recognised package manager",
        remedy=f"Supported: {', '.join(SUPPORTED_PACKAGE_MANAGERS)}. "
               "Install uv (recommended) or pip.",
    )
