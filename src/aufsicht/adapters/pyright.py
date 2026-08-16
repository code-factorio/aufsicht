"""Pyright adapter: changed-file fast gate + repo-wide per-rule ratchet
+ the strict-list growth check (v5.1 §12, §4.2–§4.4).

One tool, two gates plus one absolute check inside the full gate:

  gate "pyright"        repo-wide diagnostics grouped by rule, per-rule
                        ratchet against the merge base (v5.1 §4.3). BASE
                        source is analysed in the BASE worktree under
                        BASE's own ``pyrightconfig.json``; analyzer
                        versions always come from HEAD's
                        ``.quality/toolchain.lock`` via ctx.run — the
                        BASE worktree never chooses them (v5.1 §4.4).
  gate "pyright-fast"   changed-file list, zero errors — the speed
                        approximation, never the correctness boundary
                        (v5.1 §12): narrowing a return type breaks
                        unchanged consumers a changed-file run cannot
                        see, and the per-rule ratchet is what catches
                        those.

Import resolution is explicit. The pinned pyright runs from the
analyzer environment but analyses against the per-commit project
environment (``--pythonpath``, v5.1 §4.4). Measured with the pinned
1.1.411: without ``--pythonpath`` pyright silently falls back to the
ambient interpreter's site-packages, which makes findings depend on
whatever happens to be installed on the machine.

Strict list (v5.1 §12): a new top-level module under ``src/`` must be
added to ``pyrightconfig.json``'s ``strict`` array in the same PR, and
the list only grows. That check is absolute — it is not a ratchet and
is not lifted by §4.5 — so when it fires the gate's mechanism reports
"absolute + per-rule-ratchet" rather than misreporting an absolute
failure as a ratchet slip (v5.1 §15).

Measured output shape (pyright 1.1.411, ``--outputjson`` on stdout):

    {"version": ..., "time": ..., "generalDiagnostics": [
        {"file": "<absolute path>", "severity": "error"|"warning"|"information",
         "message": "...", "range": {"start": {"line": 0-based, ...}, ...},
         "rule": "reportArgumentType" | null | absent}],
     "summary": {"filesAnalyzed": n, "errorCount": n, ...}}

Exit 0 clean, 1 with diagnostics, anything else is a tooling error
(exit 3, never a silent pass).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .. import gitutil
from .. import ratchet as ratchet_mod
from ..errors import ToolingError
from ..model import (
    GATE_EXEMPT,
    GATE_FAIL,
    GATE_PASS,
    GATE_SKIPPED,
    MECH_ABSOLUTE,
    MECH_DIFF,
    MECH_RULE_RATCHET,
    Finding,
    GateResult,
)
from ..pipeline import GateContext, gate
from ..toolchain import project_env

PYRIGHT_CONFIG = "pyrightconfig.json"


def _python_exe(env: Path) -> Path:
    return env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _args(cwd: Path, python: Path, files: list[str] | None) -> list[str]:
    args = ["--outputjson"]
    # The config is discovered from the working directory, so the BASE
    # run in the BASE worktree automatically analyses BASE source under
    # BASE's own pyrightconfig.json (v5.1 §4.3).
    if (cwd / PYRIGHT_CONFIG).is_file():
        args += ["--project", PYRIGHT_CONFIG]
    args += ["--pythonpath", str(python)]
    return args + (files if files is not None else [])


def parse_diagnostics(json_text: str) -> tuple[list[dict], list[Finding]]:
    """(raw diagnostics, findings).

    Raw dicts keep ``rule: null`` intact so the ratchet can bucket them
    through ``ratchet.count_by_rule`` as "<no-rule>" (v5.1 §4.3);
    findings normalise the same null to the reserved bucket for
    display. Pyright lines are 0-based; Finding lines are 1-based.
    """
    try:
        data = json.loads(json_text or "{}")
    except json.JSONDecodeError as exc:
        raise ToolingError(
            f"pyright produced unparseable JSON output: {exc}",
            remedy="Run the pinned pyright by hand with --outputjson to see "
            "what it emitted; a non-JSON stream is a tooling failure, "
            "never a pass.",
        ) from exc
    raw: list[dict] = []
    findings: list[Finding] = []
    for d in data.get("generalDiagnostics", []) or []:
        if not isinstance(d, dict):
            continue
        raw.append(d)
        rule = d.get("rule") or ""
        start = (d.get("range", {}).get("start", {}) or {}).get("line", 0)
        end = (d.get("range", {}).get("end", {}) or {}).get("line", start)
        findings.append(
            Finding(
                path=str(d.get("file", "")),
                line=int(start) + 1,
                end_line=int(end) + 1,
                rule=rule if rule else ratchet_mod.NO_RULE,
                message=str(d.get("message", "")).splitlines()[0]
                if d.get("message")
                else "",
                severity=str(d.get("severity", "error")),
            )
        )
    return raw, findings


def _relative(findings: list[Finding], root: Path) -> list[Finding]:
    out: list[Finding] = []
    for f in findings:
        p = f.path
        try:
            p = str(
                (Path(p) if Path(p).is_absolute() else root / p)
                .resolve()
                .relative_to(root.resolve())
            )
        except ValueError:
            pass
        out.append(
            Finding(
                path=p,
                line=f.line,
                end_line=f.end_line,
                rule=f.rule,
                message=f.message,
                symbol=f.symbol,
                severity=f.severity,
            )
        )
    return out


def run_pyright(
    ctx: GateContext, cwd: Path, files: list[str] | None = None
) -> tuple[list[dict], list[Finding]]:
    """Run the pinned pyright in *cwd* (HEAD repo or BASE worktree).

    *files* None means repo-wide (ratchet runs); a list restricts the
    analysis to those files (fast loop). The project environment is
    built from *cwd*'s own dependency resolution — BASE's lockfile for
    the BASE run, HEAD's for HEAD (v5.1 §4.4) — while the analyzer
    binary itself always comes from HEAD's toolchain.lock via ctx.run.
    """
    env = project_env(cwd, ctx.lock, ctx.cache)
    proc = ctx.run("pyright", *_args(cwd, _python_exe(env), files), cwd=cwd)
    if proc.returncode not in (0, 1):
        raise ToolingError(
            f"pyright failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout)[:500]}",
            remedy="Check pyrightconfig.json and the pinned pyright version "
            "in .quality/toolchain.lock.",
        )
    raw, findings = parse_diagnostics(proc.stdout)
    return raw, _relative(findings, cwd)


# --- per-rule ratchet (v5.1 §4.3) ------------------------------------------


def _ratchet_counts(ctx: GateContext, cwd: Path) -> dict[str, int]:
    raw, _ = run_pyright(ctx, cwd, files=None)
    # Null/absent rule ids go through count_by_rule, which buckets them
    # as "<no-rule>" — syntax errors are ratcheted like any other rule.
    return ratchet_mod.count_by_rule([d.get("rule") for d in raw])


def _base_config_hash(repo: Path, sha: str) -> str:
    blob = ratchet_mod.read_file_at(repo, sha, PYRIGHT_CONFIG)
    if blob is None:
        return "none"
    return hashlib.sha256(blob).hexdigest()


def pyright_ratchet(ctx: GateContext) -> tuple[dict[str, int], dict[str, int], bool]:
    """(base_counts, head_counts, exempt)."""
    if ctx.is_ratchet_exempt("pyright"):
        return {}, {}, True
    key = ratchet_mod.cache_key(
        base_sha=ctx.base.sha,
        tool="pyright",
        lock_hash=ctx.lock.raw_hash,
        config_hash=_base_config_hash(ctx.repo, ctx.base.sha),
    )
    base_wt = ratchet_mod.base_worktree(ctx.repo, ctx.base.sha, ctx.cache)
    base_counts = ratchet_mod.cached_base_counts(
        key, ctx.cache, lambda: _ratchet_counts(ctx, base_wt)
    )
    head_counts = _ratchet_counts(ctx, ctx.repo)
    return base_counts, head_counts, False


# --- strict list (v5.1 §12) ------------------------------------------------


def _tree_modules(repo: Path, sha: str) -> set[str]:
    """Top-level modules under src/ at *sha* (dirs containing a .py)."""
    proc = gitutil.git(
        "ls-tree", "-r", "--name-only", f"{sha}:src", cwd=repo, check=False
    )
    if proc.returncode != 0:
        return set()
    return {
        p.split("/", 1)[0]
        for p in proc.stdout.splitlines()
        if p.endswith(".py") and "/" in p
    }


def _worktree_modules(repo: Path) -> set[str]:
    src = repo / "src"
    if not src.is_dir():
        return set()
    return {d.name for d in src.iterdir() if d.is_dir() and any(d.rglob("*.py"))}


def _strict_entries(cwd: Path) -> list[str]:
    cfg = cwd / PYRIGHT_CONFIG
    if not cfg.is_file():
        return []
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    entries = data.get("strict") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, str)]


def _normalize_entry(entry: str) -> str:
    e = entry.strip()
    while e.startswith("./"):
        e = e[2:]
    return e.rstrip("/")


def _in_strict(module: str, entries: list[str]) -> bool:
    for entry in entries:
        e = _normalize_entry(entry)
        if e in (module, f"src/{module}"):
            return True
        if e.startswith((f"src/{module}/", f"{module}/")):
            return True
    return False


def missing_strict_modules(repo: Path, base_sha: str) -> list[str]:
    """New top-level modules under src/ absent from the strict list."""
    new_modules = _worktree_modules(repo) - _tree_modules(repo, base_sha)
    entries = _strict_entries(repo)
    return sorted(m for m in new_modules if not _in_strict(m, entries))


# --- gates -----------------------------------------------------------------


@gate("pyright")
def pyright_gate(ctx: GateContext) -> GateResult:
    findings: list[Finding] = []
    extra: dict = {}
    strict_failures: list[str] = []

    for module in missing_strict_modules(ctx.repo, ctx.base.sha):
        strict_failures.append(f"src/{module}")
        findings.append(
            Finding(
                path=f"src/{module}",
                line=0,
                rule="pyright/strict-list",
                message=f'new module src/{module} is not in pyrightconfig.json\'s "strict" '
                "array — add it in this PR (v5.1 §12). The strict list only "
                "grows; removing an entry is a protected-path change.",
            )
        )

    if ctx.is_ratchet_exempt("pyright"):
        extra["ratchet"] = "exempt"
        extra["ratchet_reason"] = ctx.report.exemption_reasons.get("pyright", "exempt")
    else:
        base_counts, head_counts, _ = pyright_ratchet(ctx)
        outcome = ratchet_mod.compare(base_counts, head_counts)
        extra["ratchet"] = outcome.to_dict()
        findings += [
            Finding(
                path="(ratchet)",
                line=0,
                rule=r.rule,
                message=f"{r.rule}: base {r.base} → head {r.head} "
                "(fix what you added; do not offset it with an unrelated fix)",
            )
            for r in outcome.regressed
        ]

    if findings:
        detail = []
        if strict_failures:
            detail.append(
                "new module(s) under src/ missing from pyrightconfig.json's "
                f'"strict" list (v5.1 §12): {", ".join(strict_failures)}'
            )
        if (
            "ratchet" in extra
            and extra["ratchet"] != "exempt"
            and extra["ratchet"]["regressed_rules"]
        ):
            detail.append(
                f"per-rule ratchet regressions vs base {ctx.base.sha[:12]} (v5.1 §4.3)"
            )
        return GateResult(
            name="pyright",
            status=GATE_FAIL,
            mechanism=(
                f"{MECH_ABSOLUTE} + {MECH_RULE_RATCHET}"
                if strict_failures
                else MECH_RULE_RATCHET
            ),
            detail="; ".join(detail) or "pyright gate failed",
            findings=findings,
            extra=extra,
        )
    if ctx.is_ratchet_exempt("pyright"):
        return GateResult(
            name="pyright",
            status=GATE_EXEMPT,
            mechanism=MECH_RULE_RATCHET,
            detail=f"per-rule ratchet exempt this PR: {extra['ratchet_reason']}",
            extra=extra,
        )
    return GateResult(
        name="pyright", status=GATE_PASS, mechanism=MECH_RULE_RATCHET, extra=extra
    )


@gate("pyright-fast")
def pyright_fast_gate(ctx: GateContext) -> GateResult:
    if ctx.config.fast_pyright == "off":
        return GateResult(
            name="pyright-fast",
            status=GATE_SKIPPED,
            mechanism=MECH_DIFF,
            detail='fast.pyright = "off" in .quality/config.toml — the install-time '
            "probe measured Pyright over changed files above the fast budget "
            "and narrowed it away (v5.1 §6); the repo-wide per-rule ratchet in "
            "quality-full remains the correctness boundary (§12)",
        )
    changed = sorted(
        p
        for p in ctx.diff.changed_files
        if p.endswith(".py") and (ctx.repo / p).is_file()
    )
    if not changed:
        return GateResult(
            name="pyright-fast",
            status=GATE_PASS,
            mechanism=MECH_DIFF,
            detail="no changed Python files",
        )
    _, findings = run_pyright(ctx, ctx.repo, files=changed)
    errors = [f for f in findings if f.severity == "error"]
    if errors:
        return GateResult(
            name="pyright-fast",
            status=GATE_FAIL,
            mechanism=MECH_DIFF,
            detail=f"{len(errors)} type error(s) in changed files — the fast loop "
            "requires zero errors; this approximation never replaces the "
            "repo-wide ratchet (v5.1 §12)",
            findings=errors,
        )
    return GateResult(name="pyright-fast", status=GATE_PASS, mechanism=MECH_DIFF)
