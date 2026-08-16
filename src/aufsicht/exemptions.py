"""Ratchet exemptions (v5.1 §4.5) — tool-level by default.

    approved config change affecting Ruff     → Ruff ratchets exempt
    toolchain.lock bumps a tool               → that tool's ratchets exempt
    project lockfile changed (§4.4)           → Pyright and deptry exempt
    deptry / pyscn config unchanged           → those ratchets run normally

Rule-level narrowing (only when a config diff is confined to additions
to select/extend-select) is an explicit optimisation, not Tier 1
behaviour.

A runner upgrade is a toolchain bump one level up (distribution spec
§10): when the pinned runner_version changes between BASE and HEAD,
every ratcheted analyzer is exempt for that PR and the report says so.
"""

from __future__ import annotations

import tomllib

from . import ratchet
from .pipeline import GateContext
from .toolchain import ENVIRONMENT_SENSITIVE_TOOLS

# Config file (glob) → analyzer whose ratchet becomes incomparable when
# it changes. `.quality/config.toml` is deliberately not listed: its
# knobs are read by the runner, and a change there is a guardrail change
# the integrity gate already surfaces.
TOOL_CONFIG_GLOBS: dict[str, tuple[str, ...]] = {
    "ruff": (".quality/ruff.toml", ".ruff.toml", "ruff.toml"),
    "pyright": ("pyrightconfig.json",),
    "pyscn": (".pyscn.toml",),
    "semgrep": (".quality/semgrep/*",),
    "deptry": ("deptry.toml", ".quality/deptry.toml"),
}

# pyproject.toml sections that are tool policy (semantic-hash compared
# between BASE and HEAD — a byte diff would fire on reformatting).
TOOL_PYPROJECT_SECTIONS: dict[str, tuple[str, ...]] = {
    "ruff": ("tool.ruff",),
    "deptry": ("tool.deptry",),
}

# Tools whose per-rule ratchets exist in Tier 1.
RATCHETED_TOOLS = ("ruff", "pyright", "pyscn", "deptry", "xenon")


def _config_changed_for_tool(ctx: GateContext, tool: str) -> bool:
    import fnmatch

    globs = TOOL_CONFIG_GLOBS.get(tool, ())
    for path in ctx.diff.changed_files:
        if any(fnmatch.fnmatch(path, g) for g in globs):
            return True

    sections = TOOL_PYPROJECT_SECTIONS.get(tool, ())
    if not sections:
        return False
    head_pyproject = ctx.repo / "pyproject.toml"
    head_data: dict = {}
    if head_pyproject.is_file():
        try:
            head_data = tomllib.loads(head_pyproject.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            return True  # unparseable now, parseable then → changed
    base_bytes = ratchet.read_file_at(ctx.repo, ctx.base.sha, "pyproject.toml")
    base_data: dict = {}
    if base_bytes is not None:
        try:
            base_data = tomllib.loads(base_bytes.decode("utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            base_data = {}
    from .integrity import semantic_hash

    for section in sections:
        if _section_hash(head_data, section) != _section_hash(base_data, section):
            return True
    return False


def _section_hash(data: dict, section: str) -> str | None:
    tree = data
    for part in section.split("."):
        if not isinstance(tree, dict) or part not in tree:
            return None
        tree = tree[part]
    return semantic_hash(tree)


def _toolchain_bumps(ctx: GateContext) -> tuple[set[str], bool]:
    """(tools whose pin changed, runner_version changed)."""
    import tomllib

    head_lock = ctx.lock.tools
    base_bytes = ratchet.read_file_at(ctx.repo, ctx.base.sha, ".quality/toolchain.lock")
    base_tools: dict[str, str] = {}
    base_runner: str | None = None
    if base_bytes is not None:
        try:
            data = tomllib.loads(base_bytes.decode("utf-8"))
            base_tools = {k: v for k, v in data.get("tools", {}).items() if isinstance(v, str)}
            base_runner = data.get("runner_version")
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            base_tools = {}
    bumped = {t for t in set(head_lock) | set(base_tools) if head_lock.get(t) != base_tools.get(t)}
    runner_changed = (
        base_bytes is not None and base_runner is not None
        and base_runner != ctx.lock.runner_version
    )
    return bumped, runner_changed


def compute_exemptions(ctx: GateContext) -> None:
    """Populate report.exempt_tools / exemption_reasons /
    dependency_environment_changed (v5.1 §4.5, §4.4)."""
    reasons: dict[str, str] = {}

    for tool in RATCHETED_TOOLS:
        if _config_changed_for_tool(ctx, tool):
            reasons[tool] = f"configuration for {tool} changed in this PR"

    bumped, runner_changed = _toolchain_bumps(ctx)
    for tool in sorted(bumped):
        reasons.setdefault(tool, f".quality/toolchain.lock pin for {tool} changed")
    if runner_changed:
        for tool in RATCHETED_TOOLS:
            reasons.setdefault(
                tool,
                f"runner version changed ({ctx.lock.runner_version}) — a runner "
                "upgrade is a toolchain bump (distribution spec §10)",
            )

    from .ratchet import base_worktree
    from .toolchain import project_lockfile_differs

    base_wt = base_worktree(ctx.repo, ctx.base.sha, ctx.cache)
    if project_lockfile_differs(base_wt, ctx.repo):
        ctx.report.dependency_environment_changed = True
        for tool in sorted(ENVIRONMENT_SENSITIVE_TOOLS):
            reasons.setdefault(
                tool,
                "project lockfile changed between BASE and HEAD; the "
                "environment-sensitive comparison is unsound (v5.1 §4.4)",
            )

    ctx.report.exemption_reasons = reasons
    ctx.report.exempt_tools = sorted(reasons)
