"""Gate orchestration: build the context once, run gates in order.

The integrity check runs before every other gate (v5.1 §11.3). Gate
results accumulate into one Report; a tooling error anywhere aborts
with exit 3 — it is never downgraded to a gate failure or a pass.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .base import BaseRef, resolve_base
from .config import QualityConfig, cache_dir
from .diffmodel import build_diff
from .errors import ToolingError
from .model import DiffModel, GateResult
from .report import Report
from .toolchain import Toolchain, analyzer_env, load_toolchain, tool_exe

MODE_FAST = "fast"
MODE_FULL = "full"

# Gate order. Integrity first, always (v5.1 §11.3).
GATE_ORDER: dict[str, tuple[str, ...]] = {
    MODE_FAST: (
        "integrity", "ruff", "ruff-s", "suppressions", "complexity",
        "pyright-fast", "semgrep", "pytest",
    ),
    MODE_FULL: (
        "integrity", "ruff", "ruff-s", "suppressions", "complexity",
        "pyright", "pytest", "semgrep", "xenon", "cycles", "deadcode",
        "deptry", "pip-audit",
    ),
}

REGISTRY: dict[str, Callable[["GateContext"], GateResult]] = {}


def gate(name: str) -> Callable:
    def register(fn: Callable[["GateContext"], GateResult]) -> Callable:
        REGISTRY[name] = fn
        return fn
    return register


@dataclass
class GateContext:
    """Everything a gate needs; built once per run.

    Adapters shell out to pinned tools via :meth:`run` — the runner
    never reimplements an analyzer (distribution spec §11).
    """

    repo: Path
    config: QualityConfig
    lock: Toolchain
    base: BaseRef
    diff: DiffModel
    env: Path
    cache: Path
    report: Report
    mode: str

    def exe(self, tool: str) -> Path:
        if tool not in self.lock.tools:
            raise ToolingError(
                f"{tool} is not pinned in .quality/toolchain.lock",
                remedy="Add an exact version pin and re-run "
                       "(v5.1 §4.4: gates execute tools from the lock, and no other).",
            )
        return tool_exe(self.env, tool)

    def run(
        self,
        tool: str,
        *args: str,
        cwd: Path | None = None,
        timeout: int = 900,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [str(self.exe(tool)), *args]
        try:
            return subprocess.run(
                cmd, cwd=str(cwd or self.repo), capture_output=True,
                text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolingError(
                f"{tool} timed out after {timeout}s",
                remedy="Narrow scope or raise the tool timeout in CI; a "
                       "timeout is inconclusive, never a pass.",
            ) from exc

    def is_ratchet_exempt(self, tool: str) -> bool:
        return tool in self.report.exempt_tools


def build_context(repo: Path, mode: str) -> GateContext:
    config = QualityConfig.load(repo)
    lock = load_toolchain(repo)
    base = resolve_base(repo, config)
    diff = build_diff(repo, base.sha)
    env = analyzer_env(lock)
    return GateContext(
        repo=repo,
        config=config,
        lock=lock,
        base=base,
        diff=diff,
        env=env,
        cache=cache_dir(),
        report=Report(base=base, command=mode),
        mode=mode,
    )


def run_pipeline(repo: Path, mode: str) -> Report:
    started = time.monotonic()
    try:
        ctx = build_context(repo, mode)
    except ToolingError as exc:
        report = Report(command=mode)
        report.tooling_error = exc
        report.duration_seconds = time.monotonic() - started
        return report

    report = ctx.report

    # Ratchet exemptions (v5.1 §4.5) are computed before gates run, so
    # every ratcheted adapter can consult them.
    from .exemptions import compute_exemptions
    try:
        compute_exemptions(ctx)
    except ToolingError as exc:
        report.tooling_error = exc
        report.duration_seconds = time.monotonic() - started
        return report

    for name in GATE_ORDER[mode]:
        if name in ctx.config.disabled_gates:
            report.gates.append(
                GateResult(name=name, status="skipped", mechanism="absolute",
                           detail=f"disabled in .quality/config.toml [gates]")
            )
            continue
        fn = REGISTRY.get(name)
        if fn is None:
            report.gates.append(
                GateResult(name=name, status="skipped", mechanism="absolute",
                           detail="gate not implemented by this runner version")
            )
            continue
        try:
            report.gates.append(fn(ctx))
        except ToolingError as exc:
            report.tooling_error = exc
            break
        except Exception as exc:  # noqa: BLE001 — the report is the interface (v5.1 §15)
            # A gate crashing must still produce a JSON report with the
            # partial results and exit 3; dying with a traceback and no
            # stdout violates the contract that an agent can parse.
            report.tooling_error = ToolingError(
                f"gate {name} crashed: {type(exc).__name__}: {exc}",
                remedy="This is a runner bug or a broken analyzer "
                       "invocation — report it against the aufsicht runner "
                       f"version in the report.",
            )
            break

    report.duration_seconds = time.monotonic() - started
    return report
