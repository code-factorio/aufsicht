"""One adapter module per tool (distribution spec §4).

Adapters shell out to a pinned analyzer from the toolchain environment
and normalise its output into Findings. The runner never reimplements
an analyzer — when a gate's logic starts looking like a linter, it is
in the wrong repository (distribution spec §11).

Importing this package registers every gate (see pipeline.REGISTRY).
"""

from __future__ import annotations

from . import (  # noqa: F401
    deptry,
    pip_audit,
    pyscn,
    pytest_adapter,
    pyright,
    ruff,
    semgrep,
    suppressions,
    xenon,
)
