"""pip-audit adapter: known vulnerabilities in resolved dependencies
(v5.1 §3 table, §8, §10.1).

One gate, one tool:

  gate "pip-audit"   every known vulnerability in the project's resolved
                     dependency set, **all severities**, zero tolerance —
                     GATE_FAIL on any finding, mechanism absolute. Full
                     mode only: the network round-trips to the advisory
                     service are a CI budget, not an inner-loop cost
                     (v5.1 §5).

Why all severities and no floor: a large share of Python advisories
carry no CVSS score at all, so a severity filter would silently drop
them (§8). pip-audit's own JSON confirms this — the vulnerability
objects have id/fix_versions/aliases and **no severity field** (measured
below) — so "if present" resolves to "never", and the gate audits
everything.

Subject and environment (v5.1 §4.4): the audit target is the per-commit
project dependency environment, frozen to exact pins with
`uv pip freeze --python <projenv>/bin/python` and audited by the pinned
pip-audit from the analyzer environment (never the BASE worktree, never
the ambient interpreter). uv is the supported resolver; without it the
gate raises ToolingError rather than silently auditing nothing.

Measured against pinned pip-audit 2.10.1 and uv 0.9.26 (empirical
probes, not guessed):

  * **The progress spinner writes to stdout**. With the default
    ``--progress-spinner on`` the JSON stream is interleaved with
    spinner frames and unparseable — ``--progress-spinner off`` is
    required, not cosmetic.
  * ``--format json`` defaults ``--desc`` to on (a ~49KB payload of
    advisory markdown); ``--desc off`` keeps it ~3KB. Descriptions are
    not gate inputs.
  * JSON shape: ``{"dependencies": [{"name", "version",
    "vulns": [...]}], "fixes": []}`` — the vulnerability list key is
    **``vulns``**, and each entry is ``{"id", "fix_versions": [str],
    "aliases": [str]}`` with no severity anywhere.
  * The same advisory id can appear **more than once** per dependency
    (duplicate entries from the PyPI advisory feed); the parser dedupes
    on (package, id) so findings are not double-counted.
  * Exit codes: 0 no vulnerabilities, 1 vulnerabilities found — and 1
    *also* for a crash (bad requirement line, unknown package): a crash
    writes **no stdout at all**. Exit 1 is therefore not evidence of
    findings; an unparseable report is a ToolingError (exit 3), never a
    zero-finding pass.
  * ``--ignore-vuln ID`` matches by id *or* alias, and an unknown id is
    silently accepted — precisely why this adapter re-checks the
    ignored set against the surviving findings (alias drift, §10.1)
    instead of trusting the flag path.
  * ``--no-deps --disable-pip`` on a fully-pinned freeze produces the
    identical (package, id) result set in ~1s instead of ~7s (no
    throwaway resolution venv); it is safe here because every line of
    the freeze is an exact ``name==version`` pin of a package that was
    genuinely installed into the project env.
  * ``uv pip freeze`` emits ANSI bold escapes **even when piped**;
    ``--color never`` keeps the requirements file parseable.

Allowlist integration (v5.1 §10.1): entries with rule
``pip-audit/<id>`` become ``--ignore-vuln`` flags via
:func:`aufsicht.allowlist.ignore_vuln_flags` — every known alias
included, so an advisory-DB rename cannot silently stop matching. After
the audit the surviving findings are cross-checked against the ignored
set; an allowlisted id that still appears (the flag path failed under
us) is reported in ``extra["allowlist_alias_drift"]`` rather than
silently passing. The opposite direction — an ignored id that no longer
appears at all — is *not* drift: that is the advisory being fixed by an
upgrade, and staleness of entries is the integrity gate's expiry
business (§10), not this gate's.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..allowlist import ignore_vuln_flags, load_allowlist
from ..errors import ToolingError
from ..model import (
    GATE_FAIL,
    GATE_PASS,
    GATE_SKIPPED,
    MECH_ABSOLUTE,
    Finding,
    GateResult,
)
from ..pipeline import MODE_FULL, GateContext, gate
from ..toolchain import project_env

FREEZE_TIMEOUT_SECONDS = 300

# An exact `name==version` pin as `uv pip freeze` writes it (measured:
# plain normalized names, no markers, no editables — the project env is
# built from PyPI installs, never `-e` lines). Anything else in the
# freeze (an editable, a direct URL) cannot be audited under --no-deps
# and is dropped from the file rather than crashing the audit.
_PINNED_REQ = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==\S+$")

# Second line of defense for the measured uv behaviour (bold escapes on
# stdout even when piped): strip rather than drop, so a
# color-contaminated freeze line still yields its pin instead of
# silently removing a package from the audit subject.
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

_output_counter = itertools.count()

# The pin that pulled the vulnerable dependency — the actionable
# location even though the vulnerability itself lives in site-packages.
DEPENDENCY_PIN_PATH = "pyproject.toml"


@dataclass(frozen=True)
class VulnEntry:
    """One advisory against one pinned package, pre-normalisation."""

    name: str
    version: str
    id: str
    fix_versions: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()


def _env_python(env: Path) -> Path:
    """Interpreter of a built environment (same layout toolchain uses)."""
    return env / ("Scripts" if os.name == "nt" else "bin") / "python"


def require_uv() -> None:
    """uv is the supported resolver (v5.1 §4.4); fail loudly without it."""
    if shutil.which("uv") is None:
        raise ToolingError(
            "uv is required to freeze the project dependency environment "
            "for pip-audit, but it is not on PATH",
            remedy="Install uv (https://docs.astral.sh/uv/) and re-run; "
                   "the gate will not audit a half-resolved environment.",
        )


def pinned_lines(freeze_text: str) -> list[str]:
    """The exact ``name==version`` pins in a freeze (pure, tested)."""
    return [
        cleaned
        for cleaned in (
            _ANSI_ESCAPE.sub("", line).strip()
            for line in freeze_text.splitlines()
        )
        if _PINNED_REQ.match(cleaned)
    ]


def freeze_file_path(cache: Path) -> Path:
    """Scratch path for the frozen requirements — in the aufsicht cache,
    never inside the repository whose dependency graph the runner must
    not touch (distribution spec §3)."""
    d = cache / "pip-audit"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"freeze-{os.getpid()}-{next(_output_counter)}.txt"


def freeze_requirements(projenv: Path, dest: Path) -> list[str]:
    """Freeze the project environment at *projenv* into *dest*.

    Returns the exact pins written. Measured on uv 0.9.26: the pins go
    to stdout, the "Using Python ..." notice to stderr, and ANSI bold
    escapes appear on stdout unless ``--color never`` is passed.
    """
    cmd = [
        "uv", "pip", "freeze", "--color", "never",
        "--python", str(_env_python(projenv)),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, check=False,
            timeout=FREEZE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolingError(
            f"uv pip freeze timed out after {FREEZE_TIMEOUT_SECONDS}s",
            remedy="A hung freeze is inconclusive, never a pass; check the "
                   "project environment under the aufsicht cache.",
        ) from exc
    except OSError as exc:
        raise ToolingError(f"could not execute uv pip freeze: {exc}") from exc
    if proc.returncode != 0:
        raise ToolingError(
            f"uv pip freeze failed (exit {proc.returncode}): "
            f"{proc.stderr.strip()[:500]}",
            remedy="Check the project dependency environment and uv "
                   "installation; pip-audit audits what uv resolves.",
        )
    pins = pinned_lines(proc.stdout)
    dest.write_text("\n".join(pins) + ("\n" if pins else ""), encoding="utf-8")
    return pins


def parse_audit(json_text: str) -> list[VulnEntry]:
    """Normalise pip-audit's JSON (shape measured on 2.10.1).

    Duplicates — the same advisory id listed twice for one package —
    are collapsed; a package with no ``vulns`` key contributes nothing.
    """
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise ToolingError(
            f"pip-audit produced no parseable JSON report: {exc}",
            remedy="Measured: pip-audit exits 1 with empty stdout when it "
                   "crashes. Check its stderr in this report and the frozen "
                   "requirements file.",
        ) from exc
    if not isinstance(data, dict) or not isinstance(
        data.get("dependencies", []), list
    ):
        raise ToolingError(
            f"pip-audit JSON report has an unexpected shape: {str(data)[:200]}"
        )
    seen: set[tuple[str, str]] = set()
    vulns: list[VulnEntry] = []
    for dep in data.get("dependencies", []):
        if not isinstance(dep, dict):
            continue
        name = str(dep.get("name") or "")
        for v in dep.get("vulns") or []:
            if not isinstance(v, dict):
                continue
            vuln_id = str(v.get("id") or "")
            if not name or not vuln_id or (name, vuln_id) in seen:
                continue
            seen.add((name, vuln_id))
            vulns.append(
                VulnEntry(
                    name=name,
                    version=str(dep.get("version") or ""),
                    id=vuln_id,
                    fix_versions=tuple(str(f) for f in v.get("fix_versions") or []),
                    aliases=tuple(str(a) for a in v.get("aliases") or []),
                )
            )
    return vulns


def findings_from(vulns: list[VulnEntry]) -> list[Finding]:
    """One Finding per vulnerability; rule ``pip-audit/<id>`` (§10.1)."""
    out: list[Finding] = []
    for v in vulns:
        fixes = ", ".join(v.fix_versions) if v.fix_versions else "none published"
        parts = [
            f"{v.name} {v.version} is affected by {v.id}",
            f"fix versions: {fixes}",
            # Measured: pip-audit's JSON carries no severity field, so
            # there is nothing to filter on — the gate audits all of it.
            "no severity/CVSS reported by the advisory service",
        ]
        if v.aliases:
            parts.append("aliases: " + ", ".join(v.aliases))
        out.append(
            Finding(
                path=DEPENDENCY_PIN_PATH,
                line=0,
                end_line=0,
                rule=f"pip-audit/{v.id}",
                message="; ".join(parts),
                symbol=f"{v.name}=={v.version}",
            )
        )
    return out


def alias_drift(ignored_ids: set[str], vulns: list[VulnEntry]) -> list[str]:
    """Allowlisted vulnerabilities that survived their ignore flags.

    The flags cover every alias of every entry, and pip-audit matches
    ignores by id or alias (measured) — so a surviving intersection
    means the flag path failed: surfaced here, never silently passed
    (v5.1 §10.1). The ignored-but-absent direction is *not* drift; that
    is an advisory fixed by an upgrade (staleness is §10 expiry's
    business).
    """
    drift: list[str] = []
    for v in vulns:
        on_entry = {v.id, *v.aliases}
        hit = sorted(on_entry & ignored_ids)
        if hit:
            drift.append(
                f"allowlisted {', '.join(hit)} still reported as {v.id} "
                f"({v.name} {v.version})"
            )
    return drift


def run_pip_audit(ctx: GateContext, freeze: Path, ignore_flags: list[str]) -> list[VulnEntry]:
    """Run the pinned pip-audit against *freeze* and parse the report.

    Fail-closed by construction (v5.1 §15 taxonomy): a non-(0, 1) exit
    or an unparseable report raises ToolingError — measured, a crash
    exits 1 with empty stdout, the same code as "vulnerabilities found".
    """
    proc = ctx.run(
        "pip-audit",
        "-r", str(freeze),
        "--format", "json",
        # Measured on 2.10.1: the spinner pollutes stdout without this;
        # --desc off keeps a ~49KB advisory dump out of the gate input.
        "--progress-spinner", "off",
        "--desc", "off",
        "--aliases", "on",
        # Safe and ~7x faster on a fully-pinned freeze (measured): no
        # throwaway resolution venv, identical (package, id) result set.
        "--no-deps",
        "--disable-pip",
        *ignore_flags,
    )
    if proc.returncode not in (0, 1):
        raise ToolingError(
            f"pip-audit failed (exit {proc.returncode}): {proc.stderr[:500]}",
            remedy="Check the pip-audit pin in .quality/toolchain.lock and "
                   "network access to the vulnerability service.",
        )
    vulns = parse_audit(proc.stdout)
    if proc.returncode == 1 and not vulns:
        raise ToolingError(
            "pip-audit exited 1 (vulnerabilities found) but its JSON report "
            f"contains none — inconclusive run. stderr: {proc.stderr[:500]}",
            remedy="Re-run; if it persists, check the advisory service "
                   "reachability and the pinned pip-audit version.",
        )
    return vulns


@gate("pip-audit")
def pip_audit_gate(ctx: GateContext) -> GateResult:
    if ctx.mode != MODE_FULL:
        return GateResult(
            name="pip-audit",
            status=GATE_SKIPPED,
            mechanism=MECH_ABSOLUTE,
            detail="vulnerability audit is a full-mode CI gate, not an "
                   "inner-loop cost (v5.1 §5)",
        )
    if not ctx.config.pip_audit_enabled:
        return GateResult(
            name="pip-audit",
            status=GATE_SKIPPED,
            mechanism=MECH_ABSOLUTE,
            detail="disabled in .quality/config.toml [pip_audit]",
        )

    require_uv()
    projenv = project_env(ctx.repo, ctx.lock)

    entries = load_allowlist(ctx.repo).entries
    ignore_flags = ignore_vuln_flags(entries)
    # Exactly the ids handed to pip-audit on the command line — the
    # alias-drift cross-check compares against what was really ignored.
    ignored_ids = {flag.split("=", 1)[1] for flag in ignore_flags}

    freeze = freeze_file_path(ctx.cache)
    try:
        pins = freeze_requirements(projenv, freeze)
        if not pins:
            return GateResult(
                name="pip-audit",
                status=GATE_SKIPPED,
                mechanism=MECH_ABSOLUTE,
                detail="no dependencies to audit (the project environment "
                       "froze to zero exact pins)",
            )
        vulns = run_pip_audit(ctx, freeze, ignore_flags)
    finally:
        try:
            freeze.unlink()
        except OSError:
            pass  # scratch file in the cache; never a gate input

    extra: dict = {"audited_pins": len(pins)}
    if ignored_ids:
        extra["ignored_vulns"] = sorted(ignored_ids)
    drift = alias_drift(ignored_ids, vulns)
    if drift:
        # Visible, never silent (v5.1 §10.1): the ignore path failed for
        # these entries; the finding itself already fails the gate.
        extra["allowlist_alias_drift"] = drift

    findings = findings_from(vulns)
    if findings:
        return GateResult(
            name="pip-audit",
            status=GATE_FAIL,
            mechanism=MECH_ABSOLUTE,
            detail=f"{len(findings)} known vulnerabilit"
                   f"{'y' if len(findings) == 1 else 'ies'} in resolved "
                   "dependencies (all severities, v5.1 §8) — upgrade the pin "
                   "or record a dated entry in .quality/allowlist.toml",
            findings=findings,
            extra=extra,
        )
    return GateResult(
        name="pip-audit", status=GATE_PASS, mechanism=MECH_ABSOLUTE, extra=extra
    )
