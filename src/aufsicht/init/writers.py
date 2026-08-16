"""Write phase (distribution spec §5.1): branch, write files, never to
the default branch.

What lands in the repository is `.quality/`, dedicated dotfiles, a CI
workflow, and an AGENTS.md section — configuration and nothing else.
The repository's `pyproject.toml` dependencies and lockfile are
untouched (distribution spec §3).

What init MUST NOT do (§5.5): merge into an existing CI workflow,
rewrite AGENTS.md outside its delimiters, generate a baseline, install
unpinned tools, commit to the default branch, or write day-one
allowlist entries silently.
"""

from __future__ import annotations

import datetime as dt
import importlib.resources
import json
import shutil
from pathlib import Path

from .. import __version__
from ..errors import ToolingError
from .detect import Detection
from .pins import render_toolchain_lock

AGENTS_BEGIN = "<!-- aufsicht:begin"
AGENTS_END = "<!-- aufsicht:end -->"


def template_dir() -> Path:
    """Locate the shipped templates: inside the wheel via
    importlib.resources, or the repository checkout during development."""
    try:
        resources = importlib.resources.files("aufsicht") / "templates"
        with importlib.resources.as_file(resources) as path:
            if path.is_dir():
                return path
    except (ModuleNotFoundError, FileNotFoundError, TypeError):
        pass
    dev = Path(__file__).resolve().parent.parent.parent.parent / "templates"
    if dev.is_dir():
        return dev
    raise ToolingError(
        "templates not found next to the runner",
        remedy="This aufsicht installation is missing its templates "
               "directory — reinstall the package.",
    )


def _render_config(
    detection: Detection,
    *,
    ci_env: str | None,
    fast_pyright: str,
    fast_pytest: str,
    tests_budget_seconds: int | None,
    deployment_model: str,
) -> str:
    text = (template_dir() / "quality" / "config.toml").read_text(encoding="utf-8")
    base_ref = detection.default_branch or "main"
    text = text.replace('ref = "main"', f'ref = "{base_ref}"')
    text = text.replace(
        "# schema_version is an integer; a runner refuses — loudly — config from\n"
        "# a newer schema rather than silently misreading it (distribution §10).",
        "# schema_version is an integer; a runner refuses — loudly — config from\n"
        "# a newer schema rather than silently misreading it (distribution §10).\n"
        "#\n"
        f"# ci_env: {ci_env or 'none detected'} carries the CI base ref (v5.1 §4.6).",
    )
    text = text.replace('pyright = "changed-files"', f'pyright = "{fast_pyright}"')
    text = text.replace('pytest = "affected"', f'pytest = "{fast_pytest}"')
    if tests_budget_seconds is not None:
        text = text.replace(
            "# budget_seconds = 120",
            f"budget_seconds = {tests_budget_seconds}",
        )
    text = text.replace('deployment_model = "B"', f'deployment_model = "{deployment_model}"')
    return text


def _render_pytest_ini(detection: Detection) -> str:
    text = (template_dir() / "quality" / "pytest.ini").read_text(encoding="utf-8")
    if detection.layout == "flat":
        text = text.replace("pythonpath = src .", "pythonpath = .")
    return text


def _absorb_ruff_config(repo: Path, detection: Detection) -> str:
    """Existing root ruff.toml/.ruff.toml moves into .quality/ruff.toml
    (v5.1 §11.2) — content preserved, location corrected."""
    if detection.existing_ruff_config:
        return (repo / detection.existing_ruff_config).read_text(encoding="utf-8")
    return (template_dir() / "quality" / "ruff.toml").read_text(encoding="utf-8")


def _merge_pyrightconfig(repo: Path, detection: Detection) -> str:
    if detection.existing_pyright_config:
        try:
            data = json.loads((repo / "pyrightconfig.json").read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    else:
        data = json.loads((template_dir() / "pyrightconfig.json").read_text(encoding="utf-8"))
    data.setdefault("typeCheckingMode", "basic")
    data.setdefault("strict", [])
    data.setdefault("exclude", [])
    for extra in (".quality", ".venv"):
        if extra not in data["exclude"]:
            data["exclude"].append(extra)
    return json.dumps(data, indent=2) + "\n"


def render_workflow(runner_version: str) -> str:
    text = (template_dir() / "workflows" / "aufsicht.yml").read_text(encoding="utf-8")
    return text.replace("{{RUNNER_VERSION}}", runner_version)


def render_agents_section() -> str:
    return (template_dir() / "AGENTS.section.md").read_text(encoding="utf-8")


def write_installation(
    repo: Path,
    detection: Detection,
    *,
    ci_env: str | None,
    fast_pyright: str,
    fast_pytest: str,
    tests_budget_seconds: int | None,
    deployment_model: str,
    allowlist_toml: str | None,
    task_runner_lines: list[str],
) -> list[str]:
    """Write the whole installation. Returns the written paths (for the
    plan's expected-integrity-failure list)."""
    written: list[str] = []

    quality = repo / ".quality"
    quality.mkdir(parents=True, exist_ok=True)

    files: dict[str, str] = {
        ".quality/config.toml": _render_config(
            detection,
            ci_env=ci_env,
            fast_pyright=fast_pyright,
            fast_pytest=fast_pytest,
            tests_budget_seconds=tests_budget_seconds,
            deployment_model=deployment_model,
        ),
        ".quality/ruff.toml": _absorb_ruff_config(repo, detection),
        ".quality/pytest.ini": _render_pytest_ini(detection),
        ".quality/toolchain.lock": render_toolchain_lock(__version__),
    }
    if allowlist_toml:
        files[".quality/allowlist.toml"] = allowlist_toml

    for rel, content in files.items():
        (repo / rel).write_text(content, encoding="utf-8")
        written.append(rel)

    shutil.copytree(template_dir() / "quality" / "semgrep", quality / "semgrep",
                    dirs_exist_ok=True)
    written.append(".quality/semgrep/test-disabling.yaml")
    written.append(".quality/semgrep/trivial-asserts.yaml")
    written.append(".quality/semgrep/test-verification.yaml")

    (repo / "pyrightconfig.json").write_text(
        _merge_pyrightconfig(repo, detection), encoding="utf-8"
    )
    written.append("pyrightconfig.json")

    # Absorb an existing root ruff config by removing the old location.
    if detection.existing_ruff_config:
        old = repo / detection.existing_ruff_config
        if old.is_file():
            old.unlink()
        written.append(f"(removed {detection.existing_ruff_config}, moved into .quality/ruff.toml)")

    if detection.ci_provider == "github-actions":
        wf = repo / ".github" / "workflows" / "aufsicht.yml"
        wf.parent.mkdir(parents=True, exist_ok=True)
        wf.write_text(render_workflow(__version__), encoding="utf-8")
        written.append(".github/workflows/aufsicht.yml")
    elif detection.ci_provider is None:
        wf = repo / ".github" / "workflows" / "aufsicht.yml"
        wf.parent.mkdir(parents=True, exist_ok=True)
        wf.write_text(render_workflow(__version__), encoding="utf-8")
        written.append(".github/workflows/aufsicht.yml")

    _append_agents_section(repo)
    written.append("AGENTS.md")

    return written


def _append_agents_section(repo: Path) -> None:
    section = render_agents_section()
    agents = repo / "AGENTS.md"
    if not agents.exists():
        agents.write_text(section, encoding="utf-8")
        return
    current = agents.read_text(encoding="utf-8")
    if AGENTS_BEGIN in current and AGENTS_END in current:
        start = current.index(AGENTS_BEGIN)
        end = current.index(AGENTS_END) + len(AGENTS_END)
        agents.write_text(current[:start] + section + current[end:], encoding="utf-8")
    else:
        if not current.endswith("\n"):
            current += "\n"
        agents.write_text(current + "\n" + section, encoding="utf-8")


def propose_day_one_allowlist(
    cycles: list[tuple[tuple[str, ...], str]],
    vulnerabilities: list[str],
) -> str | None:
    """Render proposed entries — they appear in the plan with counts
    before anything is written (v5.1 §17, distribution §5.5).

    `cycles`: canonical (module ring, digest) pairs. `vulnerabilities`:
    pip-audit ids. The installer role MAY propose these; none of it
    takes effect until the installation PR is reviewed and merged
    (v5.1 §11.1)."""
    today = dt.date.today()
    if not cycles and not vulnerabilities:
        return None
    lines: list[str] = [
        "# Day-one allowlist proposed by `aufsicht init` (v5.1 §17): the",
        "# installer role may propose exceptions; a human merges the",
        "# installation PR, which is the approval act (§11.1).",
        "",
    ]
    for ring, digest in cycles:
        expires = today + dt.timedelta(days=90)
        lines += [
            "[[entry]]",
            f'rule = "cycle/{digest[:16]}"',
            f'path = "{ring[0].split(".")[-1] if ring else ""}"',
            "reason = \"Legacy import cycle present before guardrails were "
            "introduced; unblocks adoption, tracked for removal "
            "(proposed by installer).\"",
            'added_by = "installer"',
            f'added_on = "{today.isoformat()}"',
            f'expires = "{expires.isoformat()}"',
            "",
        ]
    for vuln_id in vulnerabilities:
        expires = today + dt.timedelta(days=180)
        lines += [
            "[[entry]]",
            f'rule = "pip-audit/{vuln_id}"',
            'path = "pyproject.toml"',
            "reason = \"Known advisory in a pre-existing pin; upgrading is "
            "tracked separately (proposed by installer).\"",
            'added_by = "installer"',
            f'added_on = "{today.isoformat()}"',
            f'expires = "{expires.isoformat()}"',
            "",
        ]
    return "\n".join(lines)
