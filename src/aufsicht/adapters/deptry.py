"""deptry adapter: dependency hygiene per-rule ratchet (v5.1 §3, §4.3).

One gate, one tool:

  gate "deptry"   DEP001–DEP005 findings, counted per code and compared
                  against the merge base — the §3 table's "per-rule
                  ratchet (DEP001–DEP005)". Full mode only; the gate is
                  listed in ``GATE_ORDER[MODE_FULL]`` and never runs in
                  fast mode.

deptry reads the *declared* dependencies of the checkout it runs in,
so the BASE run executes in the BASE worktree and reads BASE's
pyproject.toml — the per-commit half of v5.1 §4.4 ("BASE source
resolves from BASE's project lockfile"). The analyzer version comes
from HEAD's toolchain lock via ``ctx.run`` either way; the BASE
worktree never chooses it. deptry's one other environment input is the
DEP001/DEP003 split: a module present in the running interpreter's
site-packages but undeclared counts as "transitive" (DEP003), an
absent one as "missing" (DEP001). Both runs execute the one pinned
analyzer binary from the one analyzer environment, so that input is
held constant — exactly the §4.4 invariant — while the declared
dependency set varies per commit through cwd.

Measured against pinned deptry 0.25.1 (empirical probe, not guessed):

  * there is no ``--output-format json``; JSON goes to a *file* via
    ``-o PATH``. A clean run writes ``[]``; findings write a list of
    ``{"error": {"code", "message"}, "module",
    "location": {"file", "line", "column"}}`` — with ``line``/
    ``column`` ``null`` for pyproject-level findings (DEP002), so the
    parser must tolerate a missing line.
  * the root argument ``.`` works on the src/ layout (project name
    resolves as first-party); ``tests/`` is excluded by deptry's own
    default ``--exclude`` list, so test-only imports are not subject
    to dependency hygiene here.
  * exit codes: 0 clean, 1 findings — and 1 *also* for a crash (e.g.
    no dependency specification found), which writes no JSON file.
    A missing or unparseable output file is therefore a ToolingError
    (exit 3), never a zero-finding pass.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
from pathlib import Path

from .. import ratchet as ratchet_mod
from ..config import CONFIG_DIR
from ..errors import ToolingError
from ..model import (
    GATE_EXEMPT,
    GATE_FAIL,
    GATE_PASS,
    GATE_SKIPPED,
    MECH_RULE_RATCHET,
    Finding,
    GateResult,
)
from ..pipeline import MODE_FULL, GateContext, gate

# deptry configuration files (mirrors exemptions.TOOL_CONFIG_GLOBS):
# a change to either exempts the ratchet (v5.1 §4.5), and BASE's bytes
# key the base-run cache (§4.3: BASE source under BASE's config).
DEPTRY_CONFIGS = ("deptry.toml", f"{CONFIG_DIR}/deptry.toml")

_output_counter = itertools.count()


def _output_path(cache: Path) -> Path:
    """A scratch path for deptry's `-o` JSON, unique per invocation.

    Lives in the aufsicht cache — never inside the repository, whose
    dependency graph the runner must not touch (distribution spec §3).
    """
    d = cache / "deptry"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"deptry-{os.getpid()}-{next(_output_counter)}.json"


def parse_findings(json_text: str) -> list[Finding]:
    """Normalise deptry's JSON output (shape measured on 0.25.1)."""
    try:
        data = json.loads(json_text or "[]")
    except json.JSONDecodeError as exc:
        raise ToolingError(f"deptry produced unparseable JSON output: {exc}") from exc
    if not isinstance(data, list):
        raise ToolingError(
            f"deptry JSON output is not a list of findings: {str(data)[:200]}"
        )
    findings: list[Finding] = []
    for item in data:
        error = item.get("error") or {}
        location = item.get("location") or {}
        line = location.get("line")  # null for pyproject-level DEP002
        findings.append(
            Finding(
                path=str(location.get("file", "")),
                line=int(line) if line is not None else 0,
                end_line=int(line) if line is not None else 0,
                rule=str(error.get("code") or ""),
                message=str(error.get("message") or ""),
                symbol=str(item.get("module") or "") or None,
            )
        )
    return findings


def run_deptry(ctx: GateContext, cwd: Path) -> list[Finding]:
    """Run pinned deptry over the checkout at *cwd* and parse its JSON."""
    out = _output_path(ctx.cache)
    proc = ctx.run("deptry", ".", "--no-ansi", "-o", str(out), cwd=cwd)
    try:
        if proc.returncode not in (0, 1):
            # Measured: findings → 1, clean → 0; anything else is a
            # crash or a usage error, and exit 3 is the honest verdict.
            raise ToolingError(
                f"deptry failed (exit {proc.returncode}): {proc.stderr[:500]}",
                remedy="Check pyproject.toml's dependency sections and the "
                "pinned deptry version in .quality/toolchain.lock.",
            )
        try:
            json_text = out.read_text(encoding="utf-8")
        except OSError as exc:
            # Measured: a crashing deptry (no dependency specification
            # found, unreadable pyproject) exits 1 without writing the
            # output file. Treat as tooling error, not as zero findings.
            raise ToolingError(
                f"deptry wrote no JSON output (exit {proc.returncode}): "
                f"{proc.stderr[:500]}",
                remedy="deptry needs a [project] dependencies section or a "
                "requirements.txt in the scanned root.",
            ) from exc
        return parse_findings(json_text)
    finally:
        try:
            out.unlink()
        except OSError:
            pass  # scratch output in the cache; never a gate input


def _counts(ctx: GateContext, cwd: Path) -> dict[str, int]:
    findings = run_deptry(ctx, cwd)
    return ratchet_mod.count_by_rule([f.rule or None for f in findings])


def _base_config_hash(repo: Path, sha: str) -> str:
    """Hash of BASE's deptry config bytes ("none" when absent)."""
    for rel in DEPTRY_CONFIGS:
        blob = ratchet_mod.read_file_at(repo, sha, rel)
        if blob is not None:
            return hashlib.sha256(blob).hexdigest()
    return "none"


def deptry_ratchet(ctx: GateContext) -> tuple[dict[str, int], list[Finding], bool]:
    """(base_counts, head_findings, exempt) — the ruff pattern, with the
    HEAD findings kept (not just counted) so the gate can name modules."""
    if ctx.is_ratchet_exempt("deptry"):
        return {}, [], True
    key = ratchet_mod.cache_key(
        base_sha=ctx.base.sha,
        tool="deptry",
        lock_hash=ctx.lock.raw_hash,
        config_hash=_base_config_hash(ctx.repo, ctx.base.sha),
    )
    base_wt = ratchet_mod.base_worktree(ctx.repo, ctx.base.sha, ctx.cache)
    base_counts = ratchet_mod.cached_base_counts(
        key,
        ctx.cache,
        lambda: _counts(ctx, base_wt),
    )
    head_findings = run_deptry(ctx, ctx.repo)
    return base_counts, head_findings, False


@gate("deptry")
def deptry_gate(ctx: GateContext) -> GateResult:
    if ctx.mode != MODE_FULL:
        return GateResult(
            name="deptry",
            status=GATE_SKIPPED,
            mechanism=MECH_RULE_RATCHET,
            detail="dependency hygiene is a full-mode ratchet (v5.1 §3)",
        )

    if ctx.is_ratchet_exempt("deptry"):
        # Visible, never silent (v5.1 §15): an exemption not in the
        # report is indistinguishable from a pass.
        reason = ctx.report.exemption_reasons.get("deptry", "exempt")
        return GateResult(
            name="deptry",
            status=GATE_EXEMPT,
            mechanism=MECH_RULE_RATCHET,
            detail=f"per-rule ratchet exempt: {reason}",
            extra={"ratchet": "exempt", "ratchet_reason": reason},
        )

    base_counts, head_findings, _ = deptry_ratchet(ctx)
    head_counts = ratchet_mod.count_by_rule([f.rule or None for f in head_findings])
    outcome = ratchet_mod.compare(base_counts, head_counts)

    modules_by_code: dict[str, list[str]] = {}
    for f in head_findings:
        modules_by_code.setdefault(f.rule, []).append(f.symbol or f.path)

    findings = [
        Finding(
            path="(ratchet)",
            line=0,
            rule=f"deptry/{r.rule}",
            message=f"{r.rule}: base {r.base} → head {r.head}"
            + (
                f" ({', '.join(sorted(set(modules_by_code.get(r.rule, []))))})"
                if modules_by_code.get(r.rule)
                else ""
            )
            + " — fix what you added; do not offset it with an "
            "unrelated fix",
        )
        for r in outcome.regressed
    ]
    extra: dict = {"ratchet": outcome.to_dict()}

    if findings:
        return GateResult(
            name="deptry",
            status=GATE_FAIL,
            mechanism=MECH_RULE_RATCHET,
            detail="dependency hygiene regressed: "
            + ", ".join(f"{r.rule} {r.base} → {r.head}" for r in outcome.regressed),
            findings=findings,
            extra=extra,
        )
    return GateResult(
        name="deptry", status=GATE_PASS, mechanism=MECH_RULE_RATCHET, extra=extra
    )
