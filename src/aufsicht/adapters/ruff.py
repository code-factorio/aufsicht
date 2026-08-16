"""Ruff adapter: lint + format + enumerated S rules + C901 (v5.1 §6).

One gate family, one tool:

  gate "ruff"       lint errors and format violations in changed files
                    (diff-scoped) + per-rule count ratchet on lint
                    codes in full mode (v5.1 §4.3)
  gate "ruff-s"     enumerated S-rule findings in changed files
  gate "complexity" C901 in changed files — **changed-file scope**,
                    measured: Ruff's C901 end_location covers the def
                    line only, not the body (probe_facts.C901_SCOPE),
                    so v5.1 §4.2's fallback policy applies. We do not
                    claim changed-function semantics while implementing
                    changed-file semantics.

The S-rule list is policy and lives in the repository's
`.quality/ruff.toml`; this adapter reads it from there rather than
carrying a second copy (distribution spec §2: no policy above Layer 1
means no policy *duplicated* outside `.quality/` either).
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path

from .. import ratchet as ratchet_mod
from ..config import CONFIG_DIR
from ..errors import ToolingError
from ..model import (
    DiffModel,
    Finding,
    GateResult,
    GATE_FAIL,
    GATE_PASS,
    GATE_SKIPPED,
    MECH_DIFF,
    MECH_RULE_RATCHET,
    ScopeMode,
)
from ..pipeline import MODE_FULL, GateContext, gate
from ..scope import in_scope

RUFF_CONFIG = f"{CONFIG_DIR}/ruff.toml"


def ruff_config_path(cwd: Path) -> Path | None:
    for candidate in (cwd / RUFF_CONFIG, cwd / ".ruff.toml", cwd / "ruff.toml"):
        if candidate.is_file():
            return candidate
    return None


def _config_arg(cwd: Path) -> list[str]:
    cfg = ruff_config_path(cwd)
    return ["--config", str(cfg.relative_to(cwd))] if cfg else []


def enumerated_s_rules(cwd: Path) -> list[str]:
    """The S rules enumerated in this repo's ruff config (v5.1 §6.1)."""
    cfg = ruff_config_path(cwd)
    if cfg is None:
        return []
    try:
        data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return []
    rules: list[str] = []
    lint = data.get("lint", {})
    for key in ("select", "extend-select"):
        for code in lint.get(key, []) or []:
            if isinstance(code, str) and code.startswith("S") and code not in rules:
                rules.append(code)
    return sorted(rules)


def parse_findings(json_text: str) -> list[Finding]:
    try:
        data = json.loads(json_text or "[]")
    except json.JSONDecodeError as exc:
        raise ToolingError(f"ruff produced unparseable JSON output: {exc}") from exc
    findings: list[Finding] = []
    for m in data:
        code = m.get("code")
        findings.append(
            Finding(
                path=str(m.get("filename", "")),
                line=int(m.get("location", {}).get("row", 1)),
                end_line=int(m.get("end_location", {}).get("row", m.get("location", {}).get("row", 1))),
                rule=code or "",
                message=m.get("message", ""),
            )
        )
    return findings


def run_lint(ctx: GateContext, cwd: Path, files: list[str] | None) -> list[Finding]:
    """Run ruff check; *files* None means the whole tree (ratchet runs)."""
    args = ["check", "--output-format", "json", "--no-cache", *_config_arg(cwd)]
    args += files if files is not None else ["."]
    proc = ctx.run("ruff", *args, cwd=cwd)
    if proc.returncode not in (0, 1):
        raise ToolingError(
            f"ruff check failed (exit {proc.returncode}): {proc.stderr[:500]}",
            remedy="Check .quality/ruff.toml and the pinned ruff version in "
                   ".quality/toolchain.lock.",
        )
    return parse_findings(proc.stdout)


def run_format_check(ctx: GateContext, cwd: Path, files: list[str]) -> list[Finding]:
    if not files:
        return []
    proc = ctx.run(
        "ruff", "format", "--check", "--output-format", "json", "--no-cache",
        *_config_arg(cwd), *files, cwd=cwd,
    )
    if proc.returncode not in (0, 1):
        # Measured: ruff format --check exits 2 on an unparseable file.
        # A syntax error is a finding, not a tooling error — E999 from
        # `ruff check` already carries it, and aborting the pipeline
        # here would make the downstream pyright <no-rule> ratchet
        # unreachable (v5.1 §18).
        if proc.returncode == 2:
            return []
        raise ToolingError(
            f"ruff format --check failed (exit {proc.returncode}): {proc.stderr[:500]}",
            remedy="Check .quality/ruff.toml and the pinned ruff version.",
        )
    findings: list[Finding] = []
    for f in parse_findings(proc.stdout):
        findings.append(
            Finding(
                path=f.path, line=f.line, end_line=f.end_line, rule="format",
                message=f.message or "file is not ruff-formatted",
            )
        )
    return findings


def _changed_python_files(diff: DiffModel) -> list[str]:
    return sorted(p for p in diff.changed_files if p.endswith(".py"))


def _relative(findings: list[Finding], root: Path) -> list[Finding]:
    out = []
    for f in findings:
        p = f.path
        try:
            p = str((Path(p) if Path(p).is_absolute() else root / p).resolve().relative_to(root.resolve()))
        except ValueError:
            pass
        out.append(
            Finding(
                path=p, line=f.line, end_line=f.end_line, rule=f.rule,
                message=f.message, symbol=f.symbol, severity=f.severity,
            )
        )
    return out


def _ratchet_counts(ctx: GateContext, cwd: Path, config_files_root: Path) -> dict[str, int]:
    findings = run_lint(ctx, cwd, files=None)
    findings = _relative(findings, cwd)
    return ratchet_mod.count_by_rule([f.rule or None for f in findings])


def _base_config_hash(repo: Path, sha: str) -> str:
    blob = ratchet_mod.read_file_at(repo, sha, RUFF_CONFIG)
    if blob is None:
        return "none"
    return hashlib.sha256(blob).hexdigest()


def lint_ratchet(ctx: GateContext) -> tuple[dict[str, int], dict[str, int], bool]:
    """(base_counts, head_counts, exempt)."""
    if ctx.is_ratchet_exempt("ruff"):
        return {}, {}, True
    key = ratchet_mod.cache_key(
        base_sha=ctx.base.sha, tool="ruff",
        lock_hash=ctx.lock.raw_hash, config_hash=_base_config_hash(ctx.repo, ctx.base.sha),
    )
    base_wt = ratchet_mod.base_worktree(ctx.repo, ctx.base.sha, ctx.cache)
    base_counts = ratchet_mod.cached_base_counts(
        key, ctx.cache,
        lambda: _ratchet_counts(ctx, base_wt, base_wt),
    )
    head_counts = _ratchet_counts(ctx, ctx.repo, ctx.repo)
    return base_counts, head_counts, False


@gate("ruff")
def ruff_gate(ctx: GateContext) -> GateResult:
    changed = _changed_python_files(ctx.diff)
    findings = _relative(run_lint(ctx, ctx.repo, files=changed), ctx.repo) if changed else []
    format_findings = run_format_check(ctx, ctx.repo, changed)
    diff_findings = findings + format_findings

    mechanism = MECH_DIFF
    extra: dict = {}

    if ctx.mode == MODE_FULL:
        mechanism = f"{MECH_DIFF} + {MECH_RULE_RATCHET}"
        base_counts, head_counts, exempt = lint_ratchet(ctx)
        if exempt:
            extra["ratchet"] = "exempt"
            extra["ratchet_reason"] = ctx.report.exemption_reasons.get("ruff", "exempt")
        else:
            outcome = ratchet_mod.compare(base_counts, head_counts)
            extra["ratchet"] = outcome.to_dict()
            diff_findings = diff_findings + [
                Finding(
                    path="(ratchet)", line=0, rule=f"ruff/{r.rule}",
                    message=f"{r.rule}: base {r.base} → head {r.head} "
                             "(fix what you added; do not offset it with an unrelated fix)",
                )
                for r in outcome.regressed
            ]

    if diff_findings:
        return GateResult(
            name="ruff", status=GATE_FAIL, mechanism=mechanism,
            detail=f"{len(diff_findings)} finding(s) in changed files"
                   + (" or regressed rule buckets" if "ratchet" in extra else ""),
            findings=diff_findings, extra=extra,
        )
    return GateResult(name="ruff", status=GATE_PASS, mechanism=mechanism, extra=extra)


@gate("ruff-s")
def ruff_s_gate(ctx: GateContext) -> GateResult:
    s_rules = set(enumerated_s_rules(ctx.repo))
    if not s_rules:
        return GateResult(
            name="ruff-s", status=GATE_SKIPPED, mechanism=MECH_DIFF,
            detail="no S rules enumerated in .quality/ruff.toml (v5.1 §6.1)",
        )
    changed = _changed_python_files(ctx.diff)
    if not changed:
        return GateResult(name="ruff-s", status=GATE_PASS, mechanism=MECH_DIFF,
                          detail="no changed Python files")
    findings = _relative(run_lint(ctx, ctx.repo, files=changed), ctx.repo)
    matched = [f for f in findings if f.rule in s_rules]
    if matched:
        return GateResult(
            name="ruff-s", status=GATE_FAIL, mechanism=MECH_DIFF,
            detail=f"enumerated S-rule findings in changed files (zero tolerance)",
            findings=matched,
        )
    return GateResult(name="ruff-s", status=GATE_PASS, mechanism=MECH_DIFF)


@gate("complexity")
def complexity_gate(ctx: GateContext) -> GateResult:
    changed = _changed_python_files(ctx.diff)
    if not changed:
        return GateResult(name="complexity", status=GATE_PASS, mechanism=MECH_DIFF,
                          detail="no changed Python files")
    findings = _relative(run_lint(ctx, ctx.repo, files=changed), ctx.repo)
    # C901 scope: changed files — probe_facts.C901_SCOPE. Ruff's C901
    # end_location covers the def line only, so hunk overlap would miss
    # body edits; v5.1 §4.2 prescribes changed-file scope instead.
    matched = [f for f in findings if f.rule == "C901" and in_scope(f, ctx.diff, ScopeMode.FILE)]
    if matched:
        return GateResult(
            name="complexity", status=GATE_FAIL, mechanism=MECH_DIFF,
            detail="C901 complexity above threshold in a changed file "
                   "(changed-file scope; see probe_facts.C901_SCOPE)",
            findings=matched,
        )
    return GateResult(name="complexity", status=GATE_PASS, mechanism=MECH_DIFF)


def apply_fixes(ctx: GateContext) -> int:
    """quality-fix: ruff --fix then ruff format. MAY mutate (v5.1 §14);
    never run in validation or CI."""
    changed = _changed_python_files(ctx.diff) or ["."]
    n_fixed = 0
    proc = ctx.run("ruff", "check", "--fix", "--no-cache", *_config_arg(ctx.repo), *changed)
    n_fixed += proc.stdout.count("Fixed")
    proc = ctx.run("ruff", "format", "--no-cache", *_config_arg(ctx.repo), *changed)
    if proc.returncode == 0:
        n_fixed += proc.stdout.count("reformatted")
    return n_fixed
