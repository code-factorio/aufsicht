"""pyscn adapter: import cycles + CFG dead code (v5.1 §3 table, §10.1).

One tool, two gates with different mechanisms — the §3 table's whole
point for structural analysis:

  gate "cycles"   structured dependency analysis (§10.1 option B):
                  every circular-dependency ring is canonicalised and
                  subtracted against ``cycle/<digest[:16]>`` entries in
                  .quality/allowlist.toml; any remainder fails.
                  Absolute — not diff-scoped, not ratcheted: a cycle in
                  an untouched legacy file is still a cycle.
  gate "deadcode" CFG dead-code findings grouped by pyscn's finding
                  type (the ``reason`` field), per-type ratchet (§4.3).

Measured against the pinned pyscn 1.29.1 (see tests for the recorded
shapes):

* ``pyscn analyze --json --select <analyses> .`` always exits 0 —
  cycles and dead code do not change the exit code — and prints one
  ``report generated: <path>`` line on stdout; the JSON lives at
  ``<cwd>/.pyscn/reports/analyze_<timestamp>.json``. The timestamp has
  one-second granularity, so two runs in the same second share a
  filename: each report is read immediately after its run, never later.
* Cycles sit at ``system.DependencyAnalysis.CircularDependencies.
  CircularDependencies[]``. ``Modules[]`` is the *sorted member set*
  (a c1→c3→c2 ring reports ``[c1, c2, c3]``); the direction lives in
  the ``Dependencies[]`` From/To edges, so the ring is rebuilt from the
  edges before canonicalisation — §10.1 says "keep direction".
  ``system.Summary.CyclicDependencies`` counts edges, not cycles, and
  is not used.
* Dead-code findings sit at ``dead_code.files[].functions[].findings[]``
  with ``reason`` (the type: unreachable_after_return, …), ``severity``
  and a ``location`` whose ``file_path`` is already repo-relative. The
  analysis is CFG-based: statements after return/raise/break/continue
  and unreachable branches. It does NOT report merely-uncalled
  functions (verified with ``--min-severity info``) — the §18 row
  "unreachable function added" is therefore exercised as unreachable
  code inside a function, which is what the tool measures.
* ``.pyscn.toml`` is auto-discovered from the working directory, so
  BASE runs in the base worktree pick up BASE's pyscn configuration
  (v5.1 §4.3/§4.4) while the analyzer binary always comes from HEAD's
  toolchain lock via :meth:`GateContext.run`.
* the analysis scratch-writes ``<cwd>/.pyscn/`` next to the analysed
  code; ``full`` is read-only, so the adapter removes exactly what a
  run created (see :func:`run_analysis`).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

from .. import ratchet as ratchet_mod
from ..allowlist import canonicalize_cycle, load_allowlist
from ..errors import ToolingError
from ..model import (
    Finding,
    GateResult,
    GATE_EXEMPT,
    GATE_FAIL,
    GATE_PASS,
    MECH_ABSOLUTE,
    MECH_RULE_RATCHET,
)
from ..pipeline import GateContext, gate

PYSCN_CONFIG = ".pyscn.toml"

# The path is the rest of the line (it can contain spaces — measured);
# the emoji prefix and everything before the colon is discarded.
_REPORT_LINE = re.compile(r"report generated:\s*(.+)", re.IGNORECASE)


def _report_path(stdout: str, cwd: Path) -> Path | None:
    m = _REPORT_LINE.search(stdout)
    if m:
        path = Path(m.group(1).strip())
        return path if path.is_absolute() else cwd / path
    # Fallback when the stdout wording changes: the newest report file.
    reports = sorted(
        (cwd / ".pyscn" / "reports").glob("analyze_*.json"),
        key=lambda p: p.stat().st_mtime,
    )
    return reports[-1] if reports else None


def run_analysis(ctx: GateContext, cwd: Path, select: str) -> dict:
    """Run one ``pyscn analyze`` in *cwd* and return its JSON report.

    Read immediately: the report filename has one-second granularity and
    a later run in the same directory would overwrite it (measured).

    pyscn scratch-writes ``<cwd>/.pyscn/`` next to the code it analyses
    (gitignored in real repos, but a scratch repo has no .gitignore).
    ``full`` is a read-only command, so exactly what this run created is
    removed again: the whole ``.pyscn/`` tree when it did not exist
    before, otherwise just this run's report file.
    """
    scratch = cwd / ".pyscn"
    preexisting = scratch.exists()
    proc = ctx.run("pyscn", "analyze", "--json", "--select", select, ".", cwd=cwd)
    if proc.returncode != 0:
        raise ToolingError(
            f"pyscn analyze --select {select} failed (exit {proc.returncode}): "
            f"{proc.stderr[:500]}",
            remedy="Check .pyscn.toml and the pinned pyscn version in "
                   ".quality/toolchain.lock.",
        )
    path = _report_path(proc.stdout, cwd)
    if path is None or not path.is_file():
        raise ToolingError(
            f"pyscn analyze --select {select} produced no JSON report",
            remedy="pyscn writes <cwd>/.pyscn/reports/analyze_*.json; check "
                   "that the working tree is writable.",
        )
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ToolingError(f"pyscn report {path} is unreadable: {exc}") from exc
    if preexisting:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    else:
        shutil.rmtree(scratch, ignore_errors=True)
    return report


# --- cycles (v5.1 §10.1 option B) ------------------------------------------


def extract_cycle_rings(report: dict) -> list[list[str]]:
    """Every circular dependency as a directed ring, direction kept.

    ``Modules[]`` is sorted (measured), so the ring order is rebuilt by
    walking the ``Dependencies[]`` edges from the lexicographically
    smallest member. Only when that reconstruction fails (malformed
    edge data) do we fall back to the reported member order — still
    rotation-safe under canonicalisation, merely direction-blind.
    """
    node = (
        ((report.get("system") or {}).get("DependencyAnalysis") or {})
        .get("CircularDependencies") or {}
    )
    rings: list[list[str]] = []
    for cycle in node.get("CircularDependencies") or []:
        modules = [str(m) for m in cycle.get("Modules") or [] if str(m)]
        if not modules:
            continue
        edges: dict[str, str] = {}
        for dep in cycle.get("Dependencies") or []:
            src, dst = dep.get("From"), dep.get("To")
            if src and dst and src != dst:
                edges.setdefault(str(src), str(dst))
        ring = [min(modules)]
        seen = {ring[0]}
        current = ring[0]
        while len(ring) < len(modules):
            nxt = edges.get(current)
            if nxt is None or nxt in seen:
                break
            ring.append(nxt)
            seen.add(nxt)
            current = nxt
        rings.append(ring if len(ring) == len(modules) else modules)
    return rings


def _module_file(repo: Path, module: str) -> str | None:
    """Best-effort repo-relative file for a dotted module name."""
    parts = module.replace(".", "/")
    candidates = (
        repo / "src" / f"{parts}.py", repo / f"{parts}.py",
        repo / "src" / parts / "__init__.py", repo / parts / "__init__.py",
    )
    for candidate in candidates:
        if candidate.is_file():
            try:
                return candidate.resolve().relative_to(repo.resolve()).as_posix()
            except ValueError:
                return candidate.as_posix()
    return None


def cycle_findings(ctx: GateContext) -> tuple[list[Finding], dict]:
    """(unallowlisted cycle findings, extra) — §10.1 option B pipeline:
    extract → canonicalise → subtract allowlist → remainder fails."""
    rings = extract_cycle_rings(run_analysis(ctx, ctx.repo, "deps"))
    allowlisted_rules = {e.rule for e in load_allowlist(ctx.repo).entries}
    findings: list[Finding] = []
    n_allowlisted = 0
    seen: set[str] = set()
    for ring in rings:
        _, digest = canonicalize_cycle(list(ring))
        if digest in seen:  # the same ring reported twice is one cycle
            continue
        seen.add(digest)
        rule_id = f"cycle/{digest[:16]}"
        if rule_id in allowlisted_rules:
            n_allowlisted += 1
            continue
        findings.append(
            Finding(
                path=_module_file(ctx.repo, min(ring)) or ring[0],
                line=1,
                rule="cycle",
                message="circular import: " + " → ".join([*ring, ring[0]])
                       + f" — break the ring, or allowlist it with rule "
                         f"\"{rule_id}\" in .quality/allowlist.toml "
                         "(90-day expiry, v5.1 §10.1)",
            )
        )
    extra = {
        "cycles": {
            "total": len(rings),
            "allowlisted": n_allowlisted,
            "unallowlisted": len(findings),
        }
    }
    return findings, extra


@gate("cycles")
def cycles_gate(ctx: GateContext) -> GateResult:
    findings, extra = cycle_findings(ctx)
    if findings:
        return GateResult(
            name="cycles", status=GATE_FAIL, mechanism=MECH_ABSOLUTE,
            detail=f"{len(findings)} unallowlisted circular import(s) remain "
                   "(v5.1 §10.1 option B: zero unallowlisted cycles)",
            findings=findings, extra=extra,
        )
    allowlisted = extra["cycles"]["allowlisted"]
    return GateResult(
        name="cycles", status=GATE_PASS, mechanism=MECH_ABSOLUTE,
        detail=f"0 unallowlisted cycles ({allowlisted} allowlisted)"
               if allowlisted else None,
        extra=extra,
    )


# --- dead code (per-type ratchet, v5.1 §4.3) --------------------------------


def parse_deadcode_findings(report: dict) -> list[Finding]:
    """Normalise ``dead_code.files[].functions[].findings[]``.

    ``files`` is null when the analysis found nothing (measured).
    """
    findings: list[Finding] = []
    for file in (report.get("dead_code") or {}).get("files") or []:
        for fn in file.get("functions") or []:
            for item in fn.get("findings") or []:
                loc = item.get("location") or {}
                findings.append(
                    Finding(
                        path=str(loc.get("file_path") or file.get("file_path") or ""),
                        line=int(loc.get("start_line") or 1),
                        end_line=int(
                            loc.get("end_line") or loc.get("start_line") or 1
                        ),
                        rule=str(item.get("reason") or ""),
                        message=str(item.get("description") or item.get("reason")
                                    or "dead code"),
                        symbol=str(fn.get("name") or item.get("function_name") or "")
                        or None,
                        severity=str(item.get("severity") or "error"),
                    )
                )
    return findings


def _deadcode_counts(ctx: GateContext, cwd: Path) -> dict[str, int]:
    findings = parse_deadcode_findings(run_analysis(ctx, cwd, "deadcode"))
    # Missing/empty reasons bucket under "<no-rule>" (v5.1 §4.3).
    return ratchet_mod.count_by_rule([f.rule or None for f in findings])


def _base_pyscn_config_hash(repo: Path, sha: str) -> str:
    blob = ratchet_mod.read_file_at(repo, sha, PYSCN_CONFIG)
    if blob is None:
        return "none"
    return hashlib.sha256(blob).hexdigest()


@gate("deadcode")
def deadcode_gate(ctx: GateContext) -> GateResult:
    if ctx.is_ratchet_exempt("pyscn"):
        reason = ctx.report.exemption_reasons.get("pyscn", "exempt")
        return GateResult(
            name="deadcode", status=GATE_EXEMPT, mechanism=MECH_RULE_RATCHET,
            detail=f"pyscn ratchet exempt: {reason} (v5.1 §4.5) — the "
                   "comparison is unsound, so it is skipped visibly, not "
                   "silently passed",
            extra={"ratchet": "exempt", "ratchet_reason": reason},
        )
    key = ratchet_mod.cache_key(
        base_sha=ctx.base.sha, tool="pyscn-deadcode",
        lock_hash=ctx.lock.raw_hash,
        config_hash=_base_pyscn_config_hash(ctx.repo, ctx.base.sha),
    )
    base_wt = ratchet_mod.base_worktree(ctx.repo, ctx.base.sha, ctx.cache)
    base_counts = ratchet_mod.cached_base_counts(
        key, ctx.cache, lambda: _deadcode_counts(ctx, base_wt),
    )
    head_counts = _deadcode_counts(ctx, ctx.repo)
    outcome = ratchet_mod.compare(base_counts, head_counts)
    findings = [
        Finding(
            path="(ratchet)", line=0, rule=f"pyscn/{r.rule}",
            message=f"{r.rule}: base {r.base} → head {r.head} "
                    "(fix what you added; do not offset it with an unrelated fix)",
        )
        for r in outcome.regressed
    ]
    if findings:
        return GateResult(
            name="deadcode", status=GATE_FAIL, mechanism=MECH_RULE_RATCHET,
            detail=f"{len(findings)} dead-code type(s) regressed "
                   "(pyscn CFG analysis, grouped by finding type)",
            findings=findings, extra={"ratchet": outcome.to_dict()},
        )
    return GateResult(
        name="deadcode", status=GATE_PASS, mechanism=MECH_RULE_RATCHET,
        extra={"ratchet": outcome.to_dict()},
    )
