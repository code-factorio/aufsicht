"""Suppressions gate: added-line scan (v5.1 §3 table, §8).

Zero tolerance for suppression comments on added lines. The only
sanctioned exit is `.quality/allowlist.toml` (v5.1 §10) — there are no
inline suppressions and no per-tool ignore files.
"""

from __future__ import annotations

from ..model import GATE_FAIL, GATE_PASS, MECH_DIFF, GateResult
from ..pipeline import GateContext, gate
from ..suppression import scan_diff


@gate("suppressions")
def suppressions_gate(ctx: GateContext) -> GateResult:
    findings = scan_diff(ctx.repo, ctx.diff)
    if findings:
        return GateResult(
            name="suppressions",
            status=GATE_FAIL,
            mechanism=MECH_DIFF,
            detail="suppression comment on an added line — the only "
                   "sanctioned exit is .quality/allowlist.toml (v5.1 §10)",
            findings=findings,
        )
    return GateResult(name="suppressions", status=GATE_PASS, mechanism=MECH_DIFF)
