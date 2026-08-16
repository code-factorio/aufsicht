"""Error taxonomy.

v5.1 §15: exit codes 0 pass, 1 hard gate, 2 regression only, 3 tooling
error. Distinct codes matter — an agent must be able to tell "I broke
something" from "the linter crashed". Everything that raises
:class:`ToolingError` maps to exit 3 and MUST NOT be downgraded to a
gate failure or, worse, a pass.
"""

from __future__ import annotations


class AufsichtError(Exception):
    """Base class for all aufsicht errors."""


class ToolingError(AufsichtError):
    """A tool, git invocation or environment failed (exit 3).

    Carries an actionable ``remedy`` where one exists — the fail-closed
    cases in v5.1 §4.6 all have specific fixes (fetch-depth: 0 for
    shallow clones, etc.) and the report must say which one applies.
    """

    def __init__(self, message: str, *, remedy: str | None = None) -> None:
        super().__init__(message)
        self.remedy = remedy


class ConfigError(ToolingError):
    """.quality/ configuration is missing, unreadable or from an
    incompatible schema (exit 3)."""


class RefusalError(AufsichtError):
    """`aufsicht init` refused to act (exit 1, distribution spec §5.4).

    Distinct from a tooling error: the repository needs a human decision
    first, and the message carries the specific remedy.
    """

    def __init__(self, message: str, *, remedy: str) -> None:
        super().__init__(message)
        self.remedy = remedy
