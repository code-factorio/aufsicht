"""Machine-readable report (v5.1 §15) and exit-code decision.

`base.sha` is always present and is the commit every ratchet actually
compared against; `exempt_tools` lists any analyzer whose ratchet was
skipped this run, with `dependency_environment_changed` explaining why
when that is the cause — an exemption that is not visible in the report
is indistinguishable from a pass. The runner version and the spec
version it implements are recorded so a failure six months old is
debuggable (distribution spec §10).

Exit codes: 0 pass, 1 hard gate, 2 regression only, 3 tooling error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import ADDENDUM_VERSION, SPEC_VERSION, __version__
from .base import BaseRef
from .errors import ToolingError
from .model import GateResult

EXIT_PASS = 0
EXIT_HARD_GATE = 1
EXIT_REGRESSION_ONLY = 2
EXIT_TOOLING_ERROR = 3


@dataclass
class Report:
    gates: list[GateResult] = field(default_factory=list)
    base: BaseRef | None = None
    exempt_tools: list[str] = field(default_factory=list)
    exemption_reasons: dict[str, str] = field(default_factory=dict)
    dependency_environment_changed: bool = False
    metrics: dict[str, Any] = field(default_factory=dict)
    allowlist_expiring_within_30d: list[str] = field(default_factory=list)
    tooling_error: ToolingError | None = None
    duration_seconds: float | None = None
    command: str = "full"

    def gate(self, name: str) -> GateResult | None:
        return next((g for g in self.gates if g.name == name), None)

    @property
    def status(self) -> str:
        if self.tooling_error is not None:
            return "tooling-error"
        if any(g.status == "fail" for g in self.gates):
            return "fail"
        return "pass"

    @property
    def exit_code(self) -> int:
        if self.tooling_error is not None:
            return EXIT_TOOLING_ERROR
        failed = [g for g in self.gates if g.status == "fail"]
        if any(g.extra.get("regression_only") for g in failed):
            return EXIT_REGRESSION_ONLY
        if failed:
            return EXIT_HARD_GATE
        return EXIT_PASS

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "status": self.status,
            "tier": 1,
            "runner_version": __version__,
            "spec_version": SPEC_VERSION,
            "addendum_version": ADDENDUM_VERSION,
            "command": self.command,
            "base": (
                {"source": self.base.source, "ref": self.base.ref, "sha": self.base.sha}
                if self.base
                else None
            ),
            "dependency_environment_changed": self.dependency_environment_changed,
            "exempt_tools": sorted(self.exempt_tools),
            "exemption_reasons": self.exemption_reasons,
            "gates": {g.name: g.to_dict() for g in self.gates},
            "findings": [self._finding_dict(g, f) for g in self.gates for f in g.findings],
            "metrics": self.metrics,
            "allowlist_expiring_within_30d": self.allowlist_expiring_within_30d,
        }
        if self.duration_seconds is not None:
            d["duration_seconds"] = round(self.duration_seconds, 2)
        if self.tooling_error is not None:
            d["tooling_error"] = {
                "message": str(self.tooling_error),
                "remedy": self.tooling_error.remedy,
            }
        return d

    @staticmethod
    def _finding_dict(gate: GateResult, f) -> dict:
        return {
            "gate": gate.name,
            "rule": f.rule,
            "path": f.path,
            "symbol": f.symbol,
            "line": f.line,
            "severity": f.severity,
            "detail": f.message,
        }


def human_summary(report: Report) -> str:
    """A short, stderr-side summary; the JSON on stdout is the contract."""
    lines = []
    if report.tooling_error is not None:
        lines.append(f"TOOLING ERROR: {report.tooling_error}")
        if report.tooling_error.remedy:
            lines.append(f"  remedy: {report.tooling_error.remedy}")
    for g in report.gates:
        if g.status == "pass":
            continue
        lines.append(f"{g.status.upper():8} {g.name} ({g.mechanism})")
        if g.detail:
            lines.append(f"         {g.detail}")
        for f in g.findings[:10]:
            loc = f"{f.path}:{f.line}"
            lines.append(f"         {f.rule:24} {loc:44} {f.message[:100]}")
        if len(g.findings) > 10:
            lines.append(f"         ... and {len(g.findings) - 10} more")
    if report.exempt_tools:
        lines.append(
            "exempt ratchets: " + ", ".join(sorted(report.exempt_tools))
            + (" (project lockfile changed)" if report.dependency_environment_changed else "")
        )
    lines.append(f"status: {report.status} (exit {report.exit_code})")
    return "\n".join(lines)
