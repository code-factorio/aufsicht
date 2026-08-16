"""Core data model shared by every gate (distribution spec §12 step 1).

Findings from all analyzers normalise into :class:`Finding`; every
diff-scoped gate filters them through the single :func:`in_scope`
predicate (v5.1 §4.2 — "one filter, not three").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# Reserved bucket for findings whose tool emits no rule id (v5.1 §4.3).
# Pyright emits ``rule: null`` for syntax errors and friends; without a
# bucket those disappear from the comparison and become a free channel
# for regressions.
NO_RULE = "<no-rule>"


@dataclass(frozen=True)
class Finding:
    """One analyser finding, normalised.

    ``path`` is repo-relative with POSIX separators regardless of host
    OS. ``line``/``end_line`` are 1-based and inclusive — the range the
    analyser attached to the finding, which for Ruff is
    ``location.row..end_location.row``.
    """

    path: str
    line: int
    rule: str
    message: str
    end_line: int | None = None
    symbol: str | None = None
    severity: str = "error"

    @property
    def span(self) -> tuple[int, int]:
        end = self.end_line if self.end_line is not None else self.line
        return (self.line, max(end, self.line))


@dataclass(frozen=True)
class DiffModel:
    """The change under evaluation, from ``git diff -U0`` (v5.1 §4.2).

    ``changed_files`` includes untracked (new) files, since a finding in
    a brand-new file is definitionally in the diff. ``added_lines`` maps
    each path to inclusive line ranges added on the HEAD side; for
    untracked files it is the whole file.
    """

    changed_files: frozenset[str] = frozenset()
    added_lines: dict[str, list[tuple[int, int]]] = field(default_factory=dict)

    def added_line_numbers(self, path: str) -> set[int]:
        out: set[int] = set()
        for start, count in self.added_lines.get(path, ()):
            out.update(range(start, start + count))
        return out

    def ranges_intersect(
        self, path: str, start: int, end: int
    ) -> bool:
        """True when [start, end] intersects any added range in *path*."""
        for h_start, h_count in self.added_lines.get(path, ()):
            if start <= h_start + h_count - 1 and end >= h_start:
                return True
        return False


class ScopeMode(Enum):
    """The three diff scopes from v5.1 §4.2."""

    FILE = "file"
    LINE = "line"
    FUNCTION = "function"


# Gate statuses. "exempt" is reported, never silently dropped
# (v5.1 §15: an exemption not visible in the report is
# indistinguishable from a pass).
GATE_PASS = "pass"
GATE_FAIL = "fail"
GATE_SKIPPED = "skipped"
GATE_EXEMPT = "exempt"

# Mechanisms, exactly the vocabulary of v5.1 §4.
MECH_ABSOLUTE = "absolute"
MECH_DIFF = "diff-scoped"
MECH_RULE_RATCHET = "per-rule-ratchet"
MECH_COUNT_RATCHET = "count-ratchet"


@dataclass
class GateResult:
    """Outcome of one gate, shaped for the v5.1 §15 report."""

    name: str
    status: str
    mechanism: str
    detail: str | None = None
    findings: list[Finding] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d: dict = {"status": self.status, "mechanism": self.mechanism}
        if self.detail:
            d["detail"] = self.detail
        d.update(self.extra)
        return d
