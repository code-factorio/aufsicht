"""Probes (distribution spec §6) — turning judgment into determinism.

Global probes are answered once, baked into the runner
(probe_facts), and re-run here as assertions against the pinned
analyzer: disagreement is exit 3 and a version-bump bug, loudly, never
a silent branch.

Per-repo probes are decisions, and the decision is printed — a
sentence the installer emits, not a judgment call an agent makes and
forgets to mention. A probe that forces a narrower configuration
contributes to exit 2 (installed with warnings).
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import probe_facts
from ..errors import ToolingError


@dataclass
class ProbeReport:
    decisions: list[str] = field(default_factory=list)   # printed, in words
    warnings: list[str] = field(default_factory=list)    # narrowed config → exit 2
    measurements: dict = field(default_factory=dict)     # recorded in the plan

    def decision(self, sentence: str) -> None:
        self.decisions.append(sentence)

    def warn_narrowed(self, sentence: str) -> None:
        self.warnings.append(sentence)
        self.decisions.append(sentence)


def run_global_probe_assertions(analyzer_env: Path) -> list[str]:
    """Re-run global probes as assertions. Returns the sentences
    recorded; raises ToolingError (→ exit 3) on any disagreement."""
    recorded: list[str] = []
    ruff = analyzer_env / ("Scripts" if os.name == "nt" else "bin") / "ruff"
    probe_facts.assert_c901_span(ruff)
    recorded.append(
        "global probe ok: Ruff C901 end_location still spans the def line "
        "only, so C901 gates at changed-file scope (v5.1 §4.2)"
    )
    return recorded


def probe_pyright_cold_start(
    analyzer_env: Path, repo: Path, sample_file: Path | None, budget_seconds: float
) -> tuple[float, bool]:
    """Measure pyright cold start on this repo. Returns (seconds,
    over_budget). Under/over the quality-fast MUST decides whether the
    fast loop runs Pyright at all (v5.1 §5, §6)."""
    bin_dir = analyzer_env / ("Scripts" if os.name == "nt" else "bin")
    pyright = bin_dir / "pyright"
    target = sample_file or _first_python_file(repo)
    if target is None:
        return (0.0, False)
    started = time.monotonic()
    proc = subprocess.run(
        [str(pyright), "--outputjson", str(target)],
        cwd=str(repo), capture_output=True, text=True, timeout=300,
    )
    elapsed = time.monotonic() - started
    try:
        json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        raise ToolingError(
            f"pyright probe produced unparseable output: {proc.stderr[:300]}",
            remedy="Check the pinned pyright version in toolchain.lock.",
        )
    return (elapsed, elapsed >= budget_seconds)


def probe_pytest_wall_clock(
    project_env: Path, repo: Path, timeout: int = 300
) -> tuple[float | None, bool]:
    """Run the suite once; returns (seconds, completed). The number is
    written to config as the declared budget (v5.1 §5).

    Probes run BEFORE .quality/ exists, so the invocation cannot use
    the installed pytest.ini — equivalent flags are passed directly
    (pythonpath from the detected layout, tests/ as the target).
    """
    bin_dir = project_env / ("Scripts" if os.name == "nt" else "bin")
    python = bin_dir / "python"
    if not (repo / "tests").is_dir():
        return (None, True)
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    pythonpath = ["src", "."] if (repo / "src").is_dir() else ["."]
    env["PYTHONPATH"] = os.pathsep.join(
        [str(repo / p) for p in pythonpath]
        + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    args = [
        str(python), "-m", "pytest", "--tb=no", "-q",
        "-p", "no:cacheprovider", "--color=no", "tests",
    ]
    started = time.monotonic()
    try:
        proc = subprocess.run(
            args, cwd=str(repo), capture_output=True, text=True,
            timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        return (None, False)
    if proc.returncode in (2, 3, 4):
        # interrupted / internal / usage error — not a measurement
        return (None, False)
    return (time.monotonic() - started, True)


def probe_ci_base_env(ci_provider: str | None) -> str | None:
    """Which environment variable carries the CI base ref (v5.1 §4.6
    resolution order step 1) — verified per provider, not assumed."""
    return {
        "github-actions": "GITHUB_BASE_REF",
        "gitlab": "CI_MERGE_REQUEST_DIFF_BASE_SHA",
    }.get(ci_provider or "")


def probe_os_fork() -> bool:
    """addendum §3.1 platform capability: mutmut 3 needs fork. Tier 1 is
    unaffected; recorded for the Tier 3 decision."""
    if not hasattr(os, "fork"):
        return False
    try:
        pid = os.fork()
    except OSError:
        return False
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
    return True


def _first_python_file(repo: Path) -> Path | None:
    for base in ("src", "."):
        root = repo / base
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*.py")):
            if ".venv" in p.parts or "__pycache__" in p.parts:
                continue
            return p
    return None
