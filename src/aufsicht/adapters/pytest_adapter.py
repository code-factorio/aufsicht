"""pytest adapter: the suite must be green (v5.1 §3 "Tests | pytest |
absolute (green)", §5 budget telemetry).

One gate:

  gate "pytest"   MODE_FULL: the whole suite from the per-commit project
                  environment (v5.1 §4.4). MODE_FAST with
                  ``[fast] pytest = "affected"``: only test files
                  touching changed modules — narrowing scope, never
                  raising the budget (v5.1 §5).

Absolute mechanism: exit 0 → pass; failures → GATE_FAIL with one
Finding per ``FAILED``/``ERROR`` short-summary line. Exit 5 (no tests
collected) is neither a failure nor an error — GATE_SKIPPED; the gate
reports honestly rather than claiming credit for an empty suite.
Exit 2/3/4 (interrupted/internal/usage) raise ToolingError: an
interrupted run must not look like a pass or a fail.

Everything below is written against measured pytest 9.1.1 behaviour
(distribution spec §6 — probe first, then parse):

  * ``-c .quality/pytest.ini`` anchors rootdir AND the ini's
    ``pythonpath`` entries at the config file's own directory, so the
    template's ``pythonpath = src .`` resolves to ``.quality/src`` and
    src-layout imports abort collection with exit 2. ``--rootdir``
    fixes the nodeids but NOT ``pythonpath``. The adapter therefore
    re-resolves the ini's pythonpath against the repository root via
    PYTHONPATH — restoring the template's documented intent that
    "pythonpath makes the source importable" (templates/quality/
    pytest.ini, v5.1 §4.4); the ini's own dead entries stay in
    sys.path ahead of it and match nothing.
  * Short-summary shapes with ``--color=no --tb=no -rf -q``:
    ``FAILED <nodeid> - <reason>`` and, with ``-rE``,
    ``ERROR <nodeid>`` (collection errors carry no reason suffix).
    No line numbers exist in the summary — findings carry line 0.
  * ``--continue-on-collection-errors`` turns a collection error into
    exit 1 — a suite failure with an ``ERROR`` line — instead of
    exit 2. A broken test file is the author's own doing, so it fails
    the gate with rule ``pytest/collection``; pytest still exits 2/3
    for genuine interruptions, and those remain tooling errors.
  * ``-p no:cacheprovider`` keeps the run read-only: without it pytest
    writes ``.pytest_cache/`` into the guarded working tree, which
    then pollutes the next run's untracked-file diff (§4.2).
  * ``--color=no`` is required: pytest 9 colorises the summary lines
    even when stdout is a pipe.
"""

from __future__ import annotations

import configparser
import hashlib
import os
import re
import subprocess
import time
from pathlib import Path

from ..config import CONFIG_DIR
from ..errors import ToolingError
from ..model import (
    GATE_FAIL,
    GATE_PASS,
    GATE_SKIPPED,
    MECH_ABSOLUTE,
    DiffModel,
    Finding,
    GateResult,
)
from ..pipeline import MODE_FAST, MODE_FULL, GateContext, gate
from ..toolchain import project_env

# pytest's ExitCode enum, measured. 2/3/4 abort the run inconclusively.
EXIT_OK = 0
EXIT_TESTS_FAILED = 1
EXIT_INTERRUPTED = 2
EXIT_INTERNAL = 3
EXIT_USAGE = 4
EXIT_NO_TESTS = 5

_EXIT_MEANINGS = {
    EXIT_INTERRUPTED: "interrupted",
    EXIT_INTERNAL: "internal error",
    EXIT_USAGE: "usage error",
}

# The run is read-only; a repo may legitimately carry a slow suite
# (v5.1 §5: the project budget is repo-specific), so the hard timeout
# is generous and never the enforcement of [tests] budget_seconds —
# that one is telemetry in the report, not a gate.
DEFAULT_TIMEOUT = 1800

# Selection walk prunes these non-dotted directories alongside every
# dot-directory (.git, .quality, .venv, ...): none of them is source.
_PRUNE_DIRS = {"node_modules", "venv", "build", "dist", "__pycache__"}

_SUMMARY_LINE = re.compile(r"^(FAILED|ERROR)\s+(\S+)(?:\s+-\s+(.*))?$")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def pytest_ini_pythonpath(repo: Path) -> list[str]:
    """The ``pythonpath`` entries declared in ``.quality/pytest.ini``."""
    ini = repo / CONFIG_DIR / "pytest.ini"
    if not ini.is_file():
        return []
    parser = configparser.ConfigParser()
    try:
        parser.read(ini, encoding="utf-8")
    except configparser.Error:
        return []
    raw = parser.get("pytest", "pythonpath", fallback="").strip()
    if not raw:
        return []
    return [p for p in re.split(rf"[\s{re.escape(os.pathsep)}]+", raw) if p]


def _suite_env(repo: Path) -> dict[str, str]:
    """Environment for the suite run.

    PYTHONPATH re-resolves the ini's pythonpath entries against the
    repository root (see module docstring — with ``-c`` pytest anchors
    them at the config directory instead). Ambient PYTEST_ADDOPTS /
    PYTEST_PLUGINS are dropped: a developer's shell must not steer a
    gate (v5.1 §4.4 — the pinned environment, and no other).
    """
    env = dict(os.environ)
    # The gate is read-only (v5.1 §14): executing the suite must not
    # litter the guarded repository with __pycache__ directories.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    supplement = [str(repo / p) for p in pytest_ini_pythonpath(repo)]
    existing = env.get("PYTHONPATH", "")
    parts = supplement + ([existing] if existing else [])
    if parts:
        env["PYTHONPATH"] = os.pathsep.join(parts)
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("PYTEST_PLUGINS", None)
    return env


def _timeout_seconds(budget: int | None) -> int:
    if budget is None:
        return DEFAULT_TIMEOUT
    return max(DEFAULT_TIMEOUT, 2 * int(budget))


def run_suite(
    ctx: GateContext, paths: list[str] | None = None
) -> tuple[subprocess.CompletedProcess[str], float, Path | None]:
    """Run the suite (whole, or just *paths*) from the project env.

    Returns ``(process, wall_clock_seconds, coverage_json_path)`` — the
    coverage path is set when branch coverage ran (full mode with
    pytest-cov pinned; v5.1 §19) and points into the aufsicht cache,
    never into the guarded repository. Raises ToolingError on timeout
    and on exit 2/3/4 — never lets an interrupted run look like a pass
    or a fail.
    """
    env_dir = project_env(ctx.repo, ctx.lock, ctx.cache)
    python = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    # Config runner_args come first so the adapter's canonical flags
    # win where they collide: the summary format IS the parse contract.
    args = [
        *ctx.config.tests_runner_args,
        "-m",
        "pytest",
        "-c",
        ".quality/pytest.ini",
        "--rootdir",
        str(ctx.repo),
        "--color=no",
        "--tb=no",
        "-rfE",
        "-q",
        "-p",
        "no:cacheprovider",
        "--continue-on-collection-errors",
    ]
    suite_env = _suite_env(ctx.repo)
    coverage_json: Path | None = None
    if ctx.mode == MODE_FULL and ctx.lock.pin("pytest-cov"):
        # Branch coverage in the gate (v5.1 §19), recorded as telemetry
        # (§8): never gated at Tier 1. All coverage artifacts go to the
        # aufsicht cache — the gate is read-only (§14).
        cov_dir = ctx.cache / "coverage"
        cov_dir.mkdir(parents=True, exist_ok=True)
        tag = hashlib.sha256(str(ctx.repo).encode()).hexdigest()[:12]
        coverage_json = cov_dir / f"coverage-{tag}.json"
        source_root = "src" if (ctx.repo / "src").is_dir() else "."
        args += [
            f"--cov={source_root}",
            "--cov-branch",
            "--cov-report=",
            f"--cov-report=json:{coverage_json}",
        ]
        suite_env["COVERAGE_FILE"] = str(cov_dir / f".coverage-{tag}")
    args += list(paths or ())
    timeout = _timeout_seconds(ctx.config.tests_budget_seconds)
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [str(python), *args],
            cwd=str(ctx.repo),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=suite_env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolingError(
            f"pytest timed out after {timeout}s",
            remedy="The suite budget is repo-specific (v5.1 §5): either the "
            "suite hangs, or [tests] budget_seconds in "
            ".quality/config.toml is set too low.",
        ) from exc
    elapsed = time.monotonic() - started
    if proc.returncode not in (EXIT_OK, EXIT_TESTS_FAILED, EXIT_NO_TESTS):
        meaning = _EXIT_MEANINGS.get(proc.returncode, "unexpected exit")
        excerpt = (proc.stderr.strip() or proc.stdout.strip())[-600:]
        remedy = {
            EXIT_INTERRUPTED: "A test interrupted the run (KeyboardInterrupt or "
            "sys.exit). If it reproduces, fix the test — it "
            "must not kill the runner.",
            EXIT_USAGE: "Check .quality/pytest.ini and [tests] runner_args in "
            ".quality/config.toml.",
        }.get(
            proc.returncode, "Re-run the suite by hand to diagnose the internal error."
        )
        raise ToolingError(
            f"pytest exited {proc.returncode} ({meaning}) — the run is "
            f"inconclusive, neither a pass nor a fail. Last output: {excerpt!r}",
            remedy=remedy,
        )
    return proc, elapsed, coverage_json


def _split_nodeid(nodeid: str) -> tuple[str, str | None]:
    """``tests/test_app.py::TestK::test_x[param]`` → (path, symbol)."""
    if "::" in nodeid:
        path, _, symbol = nodeid.partition("::")
        return path.replace("\\", "/"), symbol
    return nodeid.replace("\\", "/"), None


def parse_summary(stdout: str) -> list[Finding]:
    """Findings from the ``-rfE`` short-summary lines (measured shape).

    ``FAILED <nodeid> - <reason>`` → rule ``pytest/failure``;
    ``ERROR <file>`` (no ``::``) → ``pytest/collection``;
    ``ERROR <nodeid>::<test>`` → ``pytest/error`` (setup/teardown).
    Line numbers do not exist in the summary — findings carry line 0.
    """
    findings: list[Finding] = []
    for raw in _ANSI.sub("", stdout).splitlines():
        m = _SUMMARY_LINE.match(raw.strip())
        if m is None:
            continue
        outcome, nodeid, reason = m.group(1), m.group(2), m.group(3)
        path, symbol = _split_nodeid(nodeid)
        if outcome == "FAILED":
            rule = "pytest/failure"
            message = reason or "test failed"
        elif symbol is None:
            rule = "pytest/collection"
            message = (
                reason
                or "collection error (import/syntax) — run pytest by hand for the traceback"
            )
        else:
            rule = "pytest/error"
            message = reason or "test error (setup/teardown)"
        findings.append(
            Finding(
                path=path,
                line=0,
                rule=rule,
                message=message,
                symbol=symbol,
                severity="error",
            )
        )
    return findings


def changed_src_modules(diff: DiffModel) -> list[str]:
    """Dotted module names of changed ``src/**/*.py`` files.

    ``src/scratch/app.py`` → ``scratch.app``; a package's
    ``__init__.py`` collapses to the package itself.
    """
    modules: list[str] = []
    for path in sorted(diff.changed_files):
        posix = path.replace("\\", "/")
        if not (posix.startswith("src/") and posix.endswith(".py")):
            continue
        rel = posix[len("src/") : -len(".py")].replace("/", ".")
        rel = rel.removesuffix(".__init__")
        if rel and rel not in modules:
            modules.append(rel)
    return modules


def discover_test_files(repo: Path) -> list[str]:
    """Every test file in the repository, repo-relative POSIX paths."""
    out: list[str] = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = sorted(
            d for d in dirs if not d.startswith(".") and d not in _PRUNE_DIRS
        )
        for name in sorted(files):
            if (name.startswith("test_") and name.endswith(".py")) or name.endswith(
                "_test.py"
            ):
                out.append((Path(root) / name).relative_to(repo).as_posix())
    return sorted(out)


def _references_module(text: str, module: str) -> bool:
    pattern = re.compile(rf"\b{re.escape(module)}\b")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")) and pattern.search(stripped):
            return True
    return False


def affected_test_files(repo: Path, diff: DiffModel) -> list[str]:
    """Deterministic affected-test selection for quality-fast.

    A test file is affected when it is itself in the diff, or when one
    of its import/from lines references the dotted module name of a
    changed ``src/`` file (v5.1 §5: narrow the scope, don't raise the
    budget). Textual — no collection, no plugin guessing.
    """
    modules = changed_src_modules(diff)
    selected: list[str] = []
    for test_file in discover_test_files(repo):
        if test_file in diff.changed_files:
            selected.append(test_file)
            continue
        try:
            text = (repo / test_file).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(_references_module(text, m) for m in modules):
            selected.append(test_file)
    return selected


def _coverage_percent(coverage_json: Path) -> float | None:
    """Branch+line coverage percent from a coverage.py JSON report.

    Returns None when the report is missing or empty (nothing measured
    — e.g. a repo with tests but no source): absent telemetry, not a
    zero, and never a gate.
    """
    import json as _json

    try:
        data = _json.loads(coverage_json.read_text(encoding="utf-8"))
        totals = data.get("totals", {})
    except (OSError, _json.JSONDecodeError):
        return None
    percent = totals.get("percent_covered")
    if isinstance(percent, (int, float)):
        return round(float(percent), 1)
    return None


def _budget_extra(ctx: GateContext, elapsed: float) -> dict:
    """[tests] budget_seconds is telemetry, never a gate (v5.1 §5)."""
    extra: dict = {"suite_seconds": round(elapsed, 2)}
    budget = ctx.config.tests_budget_seconds
    if budget is not None:
        extra["budget_seconds"] = budget
        if elapsed > budget:
            extra["budget_exceeded"] = True
            extra["budget_note"] = (
                f"suite took {elapsed:.1f}s against the declared budget of "
                f"{budget}s (v5.1 §5 — telemetry; fix the suite, not the budget)"
            )
    return extra


@gate("pytest")
def pytest_gate(ctx: GateContext) -> GateResult:
    extra: dict = {}
    paths: list[str] | None = None

    if ctx.mode == MODE_FAST:
        if ctx.config.fast_pytest == "off":
            return GateResult(
                name="pytest",
                status=GATE_SKIPPED,
                mechanism=MECH_ABSOLUTE,
                detail='[fast] pytest = "off" — the install probe narrowed the '
                "fast scope; tests run in quality-full only (v5.1 §5)",
            )
        paths = affected_test_files(ctx.repo, ctx.diff)
        if not paths:
            return GateResult(
                name="pytest",
                status=GATE_SKIPPED,
                mechanism=MECH_ABSOLUTE,
                detail="no affected tests",
            )
        extra["selected"] = paths

    proc, elapsed, coverage_json = run_suite(ctx, paths)
    extra.update(_budget_extra(ctx, elapsed))
    if ctx.lock.pin("pytest") is None:
        extra["pytest_pin"] = (
            "unpinned — pytest is not in .quality/toolchain.lock (v5.1 §4.4)"
        )
    if coverage_json is not None:
        covered = _coverage_percent(coverage_json)
        if covered is not None:
            # Telemetry, never gated at Tier 1 (v5.1 §8); diff coverage
            # thresholds are Tier 2.
            ctx.report.metrics.setdefault("coverage", {})["head"] = covered
            extra["branch_coverage_percent"] = covered

    if proc.returncode == EXIT_NO_TESTS:
        detail = (
            "no tests in repository — the pytest gate reports honestly"
            if ctx.mode == MODE_FULL
            else "no tests collected for the affected selection"
        )
        return GateResult(
            name="pytest",
            status=GATE_SKIPPED,
            mechanism=MECH_ABSOLUTE,
            detail=detail,
            extra=extra,
        )

    findings = parse_summary(proc.stdout)
    if proc.returncode == EXIT_TESTS_FAILED and not findings:
        # Fail closed, visibly: exit 1 with nothing parseable means the
        # summary contract broke, not that the suite passed.
        findings = [
            Finding(
                path="(suite)",
                line=0,
                rule="pytest/failure",
                message="pytest exited 1 but no FAILED/ERROR summary line was "
                "parsed — run the suite by hand to see the failure",
            )
        ]

    if findings:
        n_fail = sum(1 for f in findings if f.rule == "pytest/failure")
        n_err = len(findings) - n_fail
        detail = f"{n_fail} failed test(s)"
        if n_err:
            detail += f", {n_err} collection/error item(s)"
        return GateResult(
            name="pytest",
            status=GATE_FAIL,
            mechanism=MECH_ABSOLUTE,
            detail=detail,
            findings=findings,
            extra=extra,
        )

    return GateResult(
        name="pytest",
        status=GATE_PASS,
        mechanism=MECH_ABSOLUTE,
        detail=f"suite green ({elapsed:.2f}s)",
        extra=extra,
    )
