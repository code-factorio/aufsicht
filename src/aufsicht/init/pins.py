"""Default analyzer pins shipped by the runner.

Exact versions, never ranges (v5.1 §4.4): a range is a drifting
instrument that looks pinned. `aufsicht upgrade` is the sanctioned way
to move these, and applying it is a guardrail-change PR.
"""

from __future__ import annotations

from .. import ADDENDUM_VERSION, SPEC_VERSION

DEFAULT_TOOLCHAIN: dict[str, str] = {
    "ruff": "0.16.3",
    "pyright": "1.1.411",
    "pytest": "9.1.1",
    "pytest-cov": "7.0.0",
    "semgrep": "1.173.0",
    "xenon": "0.9.3",
    "deptry": "0.25.1",
    "pip-audit": "2.10.1",
    "pyscn": "1.29.1",
    # Tier 3 (addendum): pinned and installed only when the mutation
    # gate is enabled.
    # "mutmut": "3.7.0",
}


def render_toolchain_lock(runner_version: str) -> str:
    lines = [
        "# Analyzer pins (v5.1 §4.4). Protected path (§11.2): an agent",
        "# that can bump Ruff can shift every ratchet reference without",
        "# touching a threshold. Exact versions, never ranges.",
        "#",
        "# runner/spec identity (distribution spec §10, §13): recorded",
        "# here AND in the report so a failure six months old is",
        "# debuggable against the exact spec the runner implements.",
        "schema_version = 1",
        f'runner_version = "{runner_version}"',
        f'spec_version = "{SPEC_VERSION}"',
        f'addendum_version = "{ADDENDUM_VERSION}"',
        "",
        "[tools]",
    ]
    lines += [f'{name} = "{pin}"' for name, pin in DEFAULT_TOOLCHAIN.items()]
    return "\n".join(lines) + "\n"
