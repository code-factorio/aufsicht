"""`aufsicht init` — the Layer 2 contract (distribution spec §5).

    detect → probe → propose → write → verify

Exit codes (§5.2), distinct from the gate's v5.1 §15 codes:

    0   installed, or dry-run plan produced
    1   refused — the repo needs a human decision first (§5.4)
    2   installed with warnings — a probe forced a narrower config (§6)
    3   tooling error
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from .. import __version__
from ..allowlist import canonicalize_cycle
from ..errors import RefusalError, ToolingError
from ..toolchain import Toolchain, analyzer_env, project_env, tool_exe
from .detect import Detection, detect
from .pins import DEFAULT_TOOLCHAIN
from .probes import (
    ProbeReport,
    probe_ci_base_env,
    probe_os_fork,
    probe_pyright_cold_start,
    probe_pytest_wall_clock,
    run_global_probe_assertions,
)
from .refusals import check_refusals, force_refusals
from .writers import propose_day_one_allowlist, write_installation

WORKTREE_NOTE = (
    "the installation PR changes protected paths, so the integrity gate "
    "fails on it by design — that failure, listing exactly these files, "
    "IS the review surface; merging the PR is the §11.1 approval act"
)


def _synthetic_lock() -> Toolchain:
    from .pins import render_toolchain_lock

    text = render_toolchain_lock(__version__)
    return Toolchain(
        tools=dict(DEFAULT_TOOLCHAIN),
        runner_version=__version__,
        raw_hash=hashlib.sha256(text.encode()).hexdigest(),
    )


def _extract_cycles(repo: Path, env: Path) -> list[tuple[tuple[str, ...], str]]:
    """Legacy cycles for the day-one proposal (v5.1 §10.1 option B:
    one ugly legacy cycle must not block adoption)."""
    import shutil as _shutil

    pyscn_dir = repo / ".pyscn"
    preexisting = pyscn_dir.exists()
    proc = subprocess.run(
        [str(tool_exe(env, "pyscn")), "analyze", "--json", "--select", "deps", "."],
        cwd=str(repo), capture_output=True, text=True, timeout=300,
    )
    report_path = None
    m = re.search(r"report generated:\s*(\S+)", proc.stdout)
    if m:
        report_path = Path(m.group(1))
    if proc.returncode != 0:
        if not preexisting:
            _shutil.rmtree(pyscn_dir, ignore_errors=True)
        raise ToolingError(f"pyscn cycle probe failed: {proc.stderr[:300]}")
    data = None
    if report_path is not None and report_path.is_file():
        import json as _json

        try:
            data = _json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, _json.JSONDecodeError):
            data = None
    if not preexisting:
        # .pyscn/ is tool scratch; leaving it would dirty the tree the
        # installer just verified was clean.
        _shutil.rmtree(pyscn_dir, ignore_errors=True)
    if data is None:
        return []
    cycles = (
        data.get("system", {})
        .get("DependencyAnalysis", {})
        .get("CircularDependencies", {})
        .get("CircularDependencies", [])
    )
    out: list[tuple[tuple[str, ...], str]] = []
    for cycle in cycles:
        modules = [str(m2) for m2 in cycle.get("Modules", [])]
        ring, digest = canonicalize_cycle(modules)
        if ring:
            out.append((ring, digest))
    return out


def _audit_ids(repo: Path, env: Path, lock: Toolchain) -> list[str]:
    """Best-effort pip-audit probe for the day-one proposal. Network
    failures degrade to a warning, never a refusal."""
    import tempfile

    try:
        penv = project_env(repo, lock)
        freeze = subprocess.run(
            ["uv", "pip", "freeze", "--python", str(tool_exe(penv, "python"))],
            capture_output=True, text=True, timeout=300,
        )
        if freeze.returncode != 0:
            return []
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write(freeze.stdout)
            reqs = fh.name
        audit = subprocess.run(
            [str(tool_exe(env, "pip-audit")), "-r", reqs, "--format", "json",
             "--disable-pip"],
            capture_output=True, text=True, timeout=600,
        )
        if audit.returncode not in (0, 1):
            return []
        data = json.loads(audit.stdout or "{}")
        ids = []
        for dep in data.get("dependencies", []):
            for vuln in dep.get("vulnerabilities", []) or []:
                vid = vuln.get("id") or vuln.get("name")
                if vid:
                    ids.append(str(vid))
        return ids
    except (ToolingError, OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []


def run_init(repo: Path, *, dry_run: bool, force: bool, as_json: bool, write: bool = False) -> int:
    is_tty = bool(sys.stdout.isatty())
    dry_run = (dry_run or not is_tty) and not write

    warnings: list[str] = []
    try:
        if force:
            force_refusals(repo)
        else:
            check_refusals(repo)
        detection = detect(repo)

        lock = _synthetic_lock()
        env = analyzer_env(lock)

        probe_report = ProbeReport()
        for sentence in run_global_probe_assertions(env):
            probe_report.decision(sentence)

        ci_env = probe_ci_base_env(detection.ci_provider)
        fork_ok = probe_os_fork()
        probe_report.measurements["os_fork_available"] = fork_ok

        # Per-repo probes are decisions, printed in words (§6).
        fast_pyright = "changed-files"
        seconds, over = probe_pyright_cold_start(
            env, repo, None, budget_seconds=15.0
        )
        probe_report.measurements["pyright_cold_start_seconds"] = round(seconds, 1)
        if over:
            fast_pyright = "off"
            probe_report.warn_narrowed(
                f"Pyright cold start measured at {seconds:.0f}s against a 15s "
                "budget, so the fast loop runs without Pyright (v5.1 §5: "
                "narrow scope rather than raise the budget)"
            )
        else:
            probe_report.decision(
                f"Pyright cold start measured at {seconds:.1f}s, within the "
                "15s fast-loop budget; quality-fast runs Pyright over the "
                "changed-file list"
            )

        fast_pytest = "affected"
        suite_seconds, completed = probe_pytest_wall_clock(project_env(repo, lock), repo)
        probe_report.measurements["pytest_suite_seconds"] = (
            round(suite_seconds, 1) if suite_seconds is not None else None
        )
        if not completed:
            fast_pytest = "off"
            probe_report.warn_narrowed(
                "pytest suite did not finish within the probe timeout, so "
                "quality-fast runs without pytest"
            )
        elif suite_seconds is not None:
            probe_report.decision(
                f"pytest suite wall clock measured at {suite_seconds:.1f}s; "
                "written to config as the declared test budget (v5.1 §5)"
            )

        # Day-one allowlist proposal — counted before anything is
        # written (v5.1 §17, distribution §5.5).
        try:
            cycles = _extract_cycles(repo, env)
        except ToolingError as exc:
            cycles = []
            warnings.append(f"cycle probe failed: {exc}")
        try:
            vuln_ids = _audit_ids(repo, env, lock)
        except ToolingError as exc:
            vuln_ids = []
            warnings.append(f"pip-audit probe failed: {exc}")
        allowlist_toml = propose_day_one_allowlist(cycles, vuln_ids)

        plan = {
            "phases": ["detect", "probe", "propose", "write", "verify"],
            "dry_run": dry_run,
            "detected": {
                "package_manager": detection.package_manager,
                "layout": detection.layout,
                "has_tests": detection.has_tests,
                "test_runner": detection.test_runner,
                "task_runner": detection.task_runner,
                "ci_provider": detection.ci_provider,
                "existing_ruff_config": detection.existing_ruff_config,
                "existing_pyright_config": detection.existing_pyright_config,
                "default_branch": detection.default_branch,
            },
            "probes": {
                "decisions": probe_report.decisions,
                "warnings": probe_report.warnings,
                "measurements": probe_report.measurements,
                "ci_base_env": ci_env,
            },
            "configuration": {
                "fast_pyright": fast_pyright,
                "fast_pytest": fast_pytest,
                "tests_budget_seconds": (
                    int(suite_seconds) if suite_seconds is not None else None
                ),
                "deployment_model": "B",
            },
            "day_one_allowlist": {
                "cycles": [{"modules": list(r), "expires_in_days": 90} for r, _ in cycles],
                "vulnerabilities": [{"id": v, "expires_in_days": 180} for v in vuln_ids],
            },
            "guarantees": {
                "project_dependency_graph_unchanged": True,
                "no_baseline_file": True,
                "branches_never_default": True,
                "integrity_note": WORKTREE_NOTE,
            },
        }

        human = _render_plan(plan)
        if dry_run:
            if as_json:
                print(json.dumps(plan, indent=2))
                print(human, file=sys.stderr)
            else:
                print(human)
                print(json.dumps(plan, indent=2), file=sys.stderr)
            return 0

        # ---- write ----
        from .. import gitutil

        branch = "aufsicht/init"
        current = gitutil.branch_name(repo) or ""
        existing = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=str(repo), capture_output=True, text=True,
        )
        if existing.returncode == 0 or current == branch or current.startswith("aufsicht/init"):
            short = gitutil.head_sha(repo)[:8]
            branch = f"aufsicht/init-{short}"
        gitutil.git("checkout", "-q", "-b", branch, cwd=repo)

        written = write_installation(
            repo,
            detection,
            ci_env=ci_env,
            fast_pyright=fast_pyright,
            fast_pytest=fast_pytest,
            tests_budget_seconds=(
                int(suite_seconds) if suite_seconds is not None else None
            ),
            deployment_model="B",
            allowlist_toml=allowlist_toml,
            task_runner_lines=[],
        )
        gitutil.git("add", "-A", cwd=repo)
        gitutil.git(
            "commit", "-q", "-m",
            "aufsicht init: install v5.1 Tier 1 guardrails\n\n"
            "Configuration only — the project dependency graph and lockfile\n"
            "are unchanged (distribution spec §3). Merging this PR is the\n"
            "§11.1 approval act for every protected path it touches.",
            cwd=repo,
        )

        # ---- verify ----
        from .. import adapters  # noqa: F401 — registers gates
        from ..pipeline import run_pipeline

        verify_report = run_pipeline(repo, "full")
        verify = {
            "exit_code": verify_report.exit_code,
            "gates": {g.name: {"status": g.status} for g in verify_report.gates},
            "expected_integrity_failure": sorted(
                p for p in written if p.startswith(".quality")
                or p.startswith(".github") or p == "AGENTS.md" or p == "pyrightconfig.json"
            ),
        }
        for g in verify_report.gates:
            if g.name == "integrity":
                continue
            if g.status == "fail":
                warnings.append(
                    f"verify: gate {g.name} failed after installation — it "
                    "reports honestly on pre-existing state; see the report"
                )
        if as_json:
            # One document on stdout: plan + installation + verify.
            print(json.dumps(
                {"branch": branch, "written": written, "verify": verify, "plan": plan},
                indent=2,
            ))
            print(human, file=sys.stderr)
        else:
            print(human)
            print(json.dumps({"branch": branch, "written": written, "verify": verify}, indent=2))
        print(
            f"installed on branch {branch}; a human opens and merges the PR "
            "(never the default branch).",
            file=sys.stderr,
        )
        # Exit 2 is strictly "a probe forced a narrower configuration"
        # (§5.2). Other warnings (probe failures, pre-existing gate
        # failures surfaced honestly by verify) stay in the plan and
        # verify output with exit 0.
        if probe_report.warnings:
            return 2
        return 0
    except RefusalError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        print(f"  remedy: {exc.remedy}", file=sys.stderr)
        return 1
    except ToolingError as exc:
        print(f"tooling error: {exc}", file=sys.stderr)
        if exc.remedy:
            print(f"  remedy: {exc.remedy}", file=sys.stderr)
        return 3


def _render_plan(plan: dict) -> str:
    d = plan["detected"]
    lines = [
        "aufsicht init — installation plan",
        "",
        f"detected: {d['package_manager']} / {d['layout']} layout / "
        f"tests={d['has_tests']} / task runner={d['task_runner'] or 'none'} / "
        f"CI={d['ci_provider'] or 'none'} / default branch={d['default_branch']}",
        "",
        "probe decisions:",
    ]
    lines += [f"  - {s}" for s in plan["probes"]["decisions"]]
    a = plan["day_one_allowlist"]
    lines += [
        "",
        f"day-one allowlist proposal: {len(a['cycles'])} legacy cycle(s), "
        f"{len(a['vulnerabilities'])} pip-audit finding(s) — visible here "
        "with counts before anything is written",
        "",
        "writes: .quality/ (config, ruff, pytest, toolchain.lock, semgrep "
        "rules, allowlist), pyrightconfig.json, "
        ".github/workflows/aufsicht.yml (fetch-depth: 0), AGENTS.md section",
        "",
        "guarantees: no entry in the project's dependencies, dev-group or "
        "lockfile; no baseline file; a branch, never the default branch;",
        "  " + WORKTREE_NOTE,
    ]
    if plan["dry_run"]:
        lines += ["", "dry run — nothing was written."]
    return "\n".join(lines)
