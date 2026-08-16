"""`aufsicht upgrade` (distribution spec §10).

Upgrades never auto-migrate protected files. This command prints the
diff it would apply to `.quality/` and writes nothing. Applying it is a
guardrail-change PR under v5.1 §11.1 — an upgrade that silently
rewrites thresholds is the same hole as v5.1 §4.4's unpinned analyzer,
one level up.

A runner upgrade is a toolchain bump for ratchet purposes (v5.1 §4.5):
once applied, the affected analyzers' ratchets are exempt for that PR,
and the report says so (exemptions.compute_exemptions handles the
runner_version delta automatically).
"""

from __future__ import annotations

import difflib
import json
import sys
from pathlib import Path

from . import __version__
from .errors import ToolingError
from .init.detect import detect
from .init.writers import (
    render_agents_section,
    render_workflow,
    template_dir,
)
from .init.pins import render_toolchain_lock


def _proposed_contents(repo: Path) -> dict[str, str]:
    """What this runner version would write for each managed file,
    carrying the repository's current customised values where the
    templates accept them."""
    detection = detect(repo)
    proposals: dict[str, str] = {
        ".quality/toolchain.lock": render_toolchain_lock(__version__),
        ".quality/ruff.toml": (template_dir() / "quality" / "ruff.toml").read_text(encoding="utf-8"),
        ".quality/pytest.ini": (template_dir() / "quality" / "pytest.ini").read_text(encoding="utf-8"),
    }
    if detection.ci_provider in ("github-actions", None):
        proposals[".github/workflows/aufsicht.yml"] = render_workflow(__version__)
    proposals["AGENTS.md"] = render_agents_section()

    # config.toml: regenerate from the template, carrying the user's
    # current values for the keys the installer owns.
    config_path = repo / ".quality" / "config.toml"
    if config_path.is_file():
        proposals[".quality/config.toml"] = _carry_config(config_path, detection)
    return proposals


def _carry_config(config_path: Path, detection) -> str:
    import tomllib

    text = (template_dir() / "quality" / "config.toml").read_text(encoding="utf-8")
    try:
        current = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return text
    base = current.get("base", {})
    fast = current.get("fast", {})
    tests = current.get("tests", {})
    integrity = current.get("integrity", {})
    if isinstance(base, dict) and isinstance(base.get("ref"), str):
        text = text.replace('ref = "main"', f'ref = "{base["ref"]}"')
    if isinstance(fast, dict):
        for key, default in (("pyright", "changed-files"), ("pytest", "affected")):
            value = fast.get(key, default)
            if isinstance(value, str):
                text = text.replace(f'{key} = "{default}"', f'{key} = "{value}"')
    if isinstance(tests, dict) and isinstance(tests.get("budget_seconds"), int):
        text = text.replace("# budget_seconds = 120", f"budget_seconds = {tests['budget_seconds']}")
    if isinstance(integrity, dict) and isinstance(integrity.get("deployment_model"), str):
        text = text.replace(
            'deployment_model = "B"', f'deployment_model = "{integrity["deployment_model"]}"'
        )
    return text


def run_upgrade(repo: Path, *, as_json: bool) -> int:
    if not (repo / ".quality").is_dir():
        raise ToolingError(
            f"{repo} has no .quality/ — nothing to upgrade",
            remedy="Run `aufsicht init` first.",
        )

    proposals = _proposed_contents(repo)
    diffs: dict[str, str] = {}
    changed: list[str] = []
    for rel in sorted(proposals):
        proposed = proposals[rel]
        current_path = repo / rel
        if not current_path.is_file():
            current = ""
        else:
            current = current_path.read_text(encoding="utf-8")
        if current == proposed:
            continue
        changed.append(rel)
        diffs[rel] = "".join(difflib.unified_diff(
            current.splitlines(keepends=True),
            proposed.splitlines(keepends=True),
            fromfile=f"{rel} (installed)",
            tofile=f"{rel} (proposed by aufsicht {__version__})",
        ))

    note = (
        "A runner upgrade is a toolchain bump for ratchet purposes "
        "(v5.1 §4.5): after applying, the affected analyzers' ratchets "
        "are exempt for that PR and the report flags it."
    )
    if as_json:
        print(json.dumps({
            "runner_version": __version__,
            "would_change": changed,
            "diffs": diffs,
            "note": note,
            "writes_anything": False,
        }, indent=2))
    else:
        if not changed:
            print(f"aufsicht {__version__}: installed configuration matches; nothing to change.")
        for rel in changed:
            print(diffs[rel])
        print(f"\n{note}")
        print("Nothing was written. Applying this is a guardrail-change PR (v5.1 §11.1).")
    return 0
