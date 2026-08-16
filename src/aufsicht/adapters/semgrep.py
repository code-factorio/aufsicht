"""Semgrep adapter: anti-evasion ruleset, diff-scoped (v5.1 §3 table
rows 5-7, §9).

One gate family, one tool:

  gate "semgrep"  test disabling and trivially-passing verification on
                  ADDED LINES (ScopeMode.LINE, v5.1 §4.2 — a legacy
                  skip elsewhere in a changed file must not fire), plus
                  the verification-absence computation: a changed test
                  function (span intersecting added lines) that contains
                  no observable verification act is a violation, reported
                  as rule "test/no-verification" (v5.1 §4.2 row
                  "verification absence | changed test functions",
                  §9.1).

The ruleset is policy and lives in the repository's
`.quality/semgrep/` (distribution spec §2: no policy above Layer 1
means no policy duplicated outside `.quality/` either). Its severity
convention is part of that policy: ERROR rules are findings, the two
INFO families — ``pytest-test-function`` and ``verification-act-*`` —
are scope markers this gate consumes. The gate never re-implements a
matcher (distribution spec §11): "which acts count as verification" is
answered by the ruleset, not by Python in this module.

Measured against the pinned semgrep 1.173.0 (probe notes, /tmp scratch
runs of the real binary):

  * exit codes — 0 with findings by default; 1 with findings when
    ``--error`` is passed; 2 when a rule fails to parse. We pass
    ``--error`` so 0/1 mean "ran" and anything else is a ToolingError;
    findings are counted from the JSON, never from the exit code.
  * ``results[].check_id`` is namespaced by the config directory
    (``quality.semgrep.<id>``); rule ids are the last dot-component,
    which is unambiguous because the shipped ids contain no dots.
  * ``results[].path`` is relative to the invocation cwd;
    ``start.line``/``end.line`` are inclusive, and a function match
    INCLUDES its decorator lines (a ``@pytest.mark.skip`` added above
    an unchanged test makes it a changed test function).
  * ``extra.severity`` is ERROR/WARNING/INFO.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

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
    ScopeMode,
)
from ..pipeline import MODE_FAST, GateContext, gate
from ..scope import in_scope

RULESET_DIR = f"{CONFIG_DIR}/semgrep"

# Scope markers (INFO severity in the ruleset) — inputs to the
# verification-absence computation, never reported as findings.
TEST_MARKER = "pytest-test-function"
ACT_MARKER_PREFIX = "verification-act-"

# The computed absence violation (v5.1 §9.1). Tier 1 rule ids carry no
# tool prefix for gates whose violation the runner itself computes.
NO_VERIFICATION = "test/no-verification"

_SEVERITY = {"ERROR": "error", "WARNING": "warning", "INFO": "info"}
_DEF = re.compile(r"\b(?:async\s+)?def\s+([A-Za-z_]\w*)")


def is_marker(rule: str) -> bool:
    """True for the ruleset's INFO scope markers (v5.1 §9.1)."""
    return rule == TEST_MARKER or rule.startswith(ACT_MARKER_PREFIX)


def _repo_relative(path: str, root: Path) -> str:
    p = Path(path)
    try:
        p = (p if p.is_absolute() else root / p).resolve().relative_to(root.resolve())
    except ValueError:
        return str(p)
    return p.as_posix()


def parse_findings(json_text: str, root: Path) -> list[Finding]:
    """Normalise `semgrep --json` results[] into Findings.

    ``check_id`` keeps only its last dot-component (the config-directory
    namespace is an invocation detail, not the rule id).
    """
    try:
        data = json.loads(json_text or "{}")
    except json.JSONDecodeError as exc:
        raise ToolingError(f"semgrep produced unparseable JSON output: {exc}") from exc
    findings: list[Finding] = []
    for r in data.get("results", []) or []:
        start = r.get("start", {})
        end = r.get("end", {})
        rule = str(r.get("check_id", "")).rsplit(".", 1)[-1]
        findings.append(
            Finding(
                path=_repo_relative(str(r.get("path", "")), root),
                line=int(start.get("line", 1)),
                end_line=int(end.get("line", start.get("line", 1))),
                rule=rule,
                message=str(r.get("extra", {}).get("message", "")),
                severity=_SEVERITY.get(str(r.get("extra", {}).get("severity", "")), "error"),
            )
        )
    return findings


def run_semgrep(ctx: GateContext, targets: list[str]) -> list[Finding]:
    """Run the pinned semgrep over *targets* (repo-relative paths)."""
    proc = ctx.run(
        "semgrep", "--config", RULESET_DIR, "--json", "--error", "--metrics=off",
        *targets, cwd=ctx.repo,
    )
    if proc.returncode not in (0, 1):
        raise ToolingError(
            f"semgrep failed (exit {proc.returncode}): {(proc.stderr or proc.stdout)[:500]}",
            remedy="Check the .quality/semgrep rules and the pinned semgrep "
                   "version in .quality/toolchain.lock (exit 2 means a rule "
                   "failed to parse).",
        )
    return parse_findings(proc.stdout, ctx.repo)


def _function_symbol(path: Path, start: int, end: int) -> str | None:
    """Name of the function beginning in [start, end] (decorators
    precede the def line, which is inside the match span)."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    m = _DEF.search("\n".join(lines[start - 1:end]))
    return m.group(1) if m else None


def no_verification_findings(
    findings: list[Finding], diff: DiffModel, repo: Path
) -> list[Finding]:
    """Changed test functions with no overlapping verification act.

    A test function is *changed* when its match span — decorators
    included, measured above — intersects any added line (v5.1 §4.2
    "changed test functions"). An act covers the function when a
    ``verification-act-*`` match in the same file overlaps that span;
    both matches carry whole-function spans, so the overlap is exact.
    """
    acts: dict[str, list[Finding]] = {}
    for f in findings:
        if f.rule.startswith(ACT_MARKER_PREFIX):
            acts.setdefault(f.path, []).append(f)

    violations: list[Finding] = []
    for fn in findings:
        if fn.rule != TEST_MARKER:
            continue
        if not in_scope(fn, diff, ScopeMode.FUNCTION):
            continue
        start, end = fn.span
        if any(a.line <= end and a.span[1] >= start for a in acts.get(fn.path, ())):
            continue
        symbol = _function_symbol(repo / fn.path, start, end)
        violations.append(
            Finding(
                path=fn.path,
                line=fn.line,
                end_line=fn.end_line,
                rule=NO_VERIFICATION,
                symbol=symbol,
                message=(
                    f"test function {symbol or '(anonymous)'} verifies nothing: its "
                    "body contains no observable verification act (assert, "
                    "pytest.raises/warns/deprecated_call, self.assert*/self.fail, "
                    "assert_*/check_*/verify_* helper, mock assertion or snapshot "
                    "— v5.1 §9.1). Add a real check, or add your project's "
                    "assertion helper to [test_verification] calls instead of "
                    "suppressing this."
                ),
            )
        )
    return violations


def _changed_python_files(diff: DiffModel) -> list[str]:
    return sorted(p for p in diff.changed_files if p.endswith(".py"))


@gate("semgrep")
def semgrep_gate(ctx: GateContext) -> GateResult:
    if ctx.mode == MODE_FAST and ctx.config.fast_semgrep == "off":
        return GateResult(
            name="semgrep", status=GATE_SKIPPED, mechanism=MECH_DIFF,
            detail="[fast] semgrep = off in .quality/config.toml (probe-narrowed, v5.1 §5)",
        )
    if not (ctx.repo / RULESET_DIR).is_dir():
        # A missing ruleset is an init/integrity matter (.quality/** is
        # protected, v5.1 §11.2); this gate reports its absence loudly
        # rather than silently passing.
        return GateResult(
            name="semgrep", status=GATE_SKIPPED, mechanism=MECH_DIFF,
            detail=f"no {RULESET_DIR}/ ruleset — run `aufsicht init` to ship the "
                   "anti-evasion rules (v5.1 §9)",
        )

    targets = _changed_python_files(ctx.diff)
    if not targets:
        return GateResult(
            name="semgrep", status=GATE_PASS, mechanism=MECH_DIFF,
            detail="no changed Python files",
        )

    findings = run_semgrep(ctx, targets)

    # ERROR rules: added-line scope, zero tolerance (v5.1 §4.2, §9.2) —
    # a legacy skip elsewhere in a changed file must NOT fire.
    added_line = [
        f for f in findings
        if not is_marker(f.rule) and in_scope(f, ctx.diff, ScopeMode.LINE)
    ]
    missing = no_verification_findings(findings, ctx.diff, ctx.repo)
    violations = added_line + missing

    if violations:
        return GateResult(
            name="semgrep", status=GATE_FAIL, mechanism=MECH_DIFF,
            detail=(
                f"{len(violations)} anti-evasion finding(s): "
                f"{len(added_line)} on added lines (test disabling / trivial "
                f"assertions, v5.1 §9.2), {len(missing)} changed test function(s) "
                "verifying nothing (v5.1 §9.1)"
            ),
            findings=violations,
            extra={"files_scanned": len(targets)},
        )
    return GateResult(
        name="semgrep", status=GATE_PASS, mechanism=MECH_DIFF,
        extra={"files_scanned": len(targets)},
    )
