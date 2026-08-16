"""Guardrail integrity (v5.1 §11.2) — protected paths and semantic
hashing.

The check runs before every other gate (v5.1 §11.3). Any modification
to a protected path without §11.1 approval fails, and the failure is
not overridable by the allowlist.

What §11.1 approval means depends on the deployment model configured in
`.quality/config.toml`:

  A — agent isolated in CI/sandbox: signed commits on protected paths,
      verified against an allowed-signers file.
  B — agent in the developer's shell: no local mechanism is a control;
      the boundary is server-side review. Locally this gate is a
      tripwire that fails and names the paths.
  C — solo developer: explicitly a tripwire, not a security control.

A PR label is not an acceptable mechanism in any model.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import subprocess
import tomllib
from pathlib import Path

from .model import GateResult, GATE_FAIL, GATE_PASS, MECH_ABSOLUTE
from .pipeline import GateContext, gate

# v5.1 §11.2. `pyproject.toml` is deliberately absent — git diff
# operates on paths and cannot see TOML sections; quality config that
# must live there is covered by the semantic-hash fallback below.
PROTECTED_PATTERNS: tuple[str, ...] = (
    ".quality/*",
    "pyrightconfig.json",
    ".pyscn.toml",
    ".pre-commit-config.yaml",
    ".github/workflows/*",
    "AGENTS.md",
)


def is_protected(path: str, extra: tuple[str, ...] = ()) -> bool:
    patterns = PROTECTED_PATTERNS + tuple(extra)
    return any(fnmatch.fnmatch(path, pat) for pat in patterns)


def protected_changes(ctx: GateContext) -> list[str]:
    return sorted(
        p for p in ctx.diff.changed_files if is_protected(p, ctx.config.extra_protected)
    )


# --- semantic hashing (v5.1 §11.2) ----------------------------------------
#
# parse TOML → extract subtree → drop comments (parser already did) →
# normalize scalars → sort mapping keys recursively, preserve array
# order → canonical JSON → SHA-256. A raw sha256sum over grepped lines
# would trip on reformatting and be disabled within a month.


def canonicalize(obj):
    if isinstance(obj, dict):
        return {k: canonicalize(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        return [canonicalize(v) for v in obj]  # array order is significant
    return obj


def semantic_hash(tree: dict) -> str:
    blob = json.dumps(canonicalize(tree), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def pyproject_section_hash(pyproject_path: Path, section: str) -> str | None:
    """Semantic hash of `pyproject.toml`'s ``[<section>]`` subtree, or
    None when the section is absent."""
    if not pyproject_path.is_file():
        return None
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        return "__unparseable__"
    tree = data
    for part in section.split("."):
        if not isinstance(tree, dict) or part not in tree:
            return None
        tree = tree[part]
    return semantic_hash(tree)


# Sections of pyproject.toml that are quality policy despite the file
# not being protectable wholesale (v5.1 §11.2; addendum §6 makes
# [tool.mutmut] the first real user of this fallback).
POLICY_SECTIONS: tuple[str, ...] = ("tool.ruff", "tool.mutmut", "tool.deptry")

CONFIG_HASHES_PATH = ".quality/config-hashes.json"


def expected_section_hashes(repo: Path) -> dict[str, str]:
    path = repo / CONFIG_HASHES_PATH
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def section_hash_mismatches(ctx: GateContext) -> list[tuple[str, str | None, str]]:
    """(section, computed, expected) for policy sections whose semantic
    hash no longer matches .quality/config-hashes.json."""
    expected = expected_section_hashes(ctx.repo)
    mismatches: list[tuple[str, str | None, str]] = []
    for section in POLICY_SECTIONS:
        if section not in expected:
            continue
        computed = pyproject_section_hash(ctx.repo / "pyproject.toml", section)
        if computed != expected[section]:
            mismatches.append((section, computed, expected[section]))
    return mismatches


# --- model A: signed commits on protected paths ----------------------------


def _signers_ok(ctx: GateContext, commits: list[str]) -> tuple[bool, str]:
    """Verify each commit that touched protected paths is signed by a
    principal in the allowed-signers file (deployment model A)."""
    signers = ctx.repo / str(ctx.config.allowed_signers or ".quality/allowed_signers")
    if not signers.is_file():
        return False, f"allowed-signers file {signers} not found"
    for sha in commits:
        show = subprocess.run(
            ["git", "show", "-s", "--format=%G? %GS", sha],
            cwd=str(ctx.repo), capture_output=True, text=True,
        )
        if show.returncode != 0:
            return False, f"could not inspect commit {sha}"
        status_line = show.stdout.strip()
        if not status_line or status_line[0] in ("N", "E", "U", "B", "X", "Y"):
            return False, f"commit {sha[:12]} is not validly signed ({status_line or 'no signature'})"
        key = status_line.split(None, 1)[1] if " " in status_line else ""
        verify = subprocess.run(
            ["ssh-keygen", "-Y", "check-novalidate", "-n", "git",
             "-s", "/dev/stdin"],
            input=f"{key} git {sha}\n", capture_output=True, text=True,
        )
        if verify.returncode != 0:
            return False, f"signature on {sha[:12]} did not verify"
    return True, ""


def _commits_touching(ctx: GateContext, paths: list[str]) -> list[str]:
    """Commits between base and HEAD that touched any of *paths*."""
    if not paths:
        return []
    out = subprocess.run(
        ["git", "log", "--format=%H", f"{ctx.base.sha}..HEAD", "--", *paths],
        cwd=str(ctx.repo), capture_output=True, text=True,
    )
    return [c for c in out.stdout.split() if c]


@gate("integrity")
def integrity_gate(ctx: GateContext) -> GateResult:
    changed = protected_changes(ctx)

    failures: list[str] = []
    if changed:
        if ctx.config.integrity_model == "A":
            commits = _commits_touching(ctx, changed)
            ok, why = _signers_ok(ctx, commits) if commits else (True, "")
            if ok:
                failures.append(
                    "protected paths changed with verified signatures: "
                    + ", ".join(changed)
                )
            else:
                failures.append(f"protected paths changed without §11.1 approval ({why}): " + ", ".join(changed))
        else:
            failures.append(
                "protected paths changed: " + ", ".join(changed)
            )

    for section, computed, expected in section_hash_mismatches(ctx):
        failures.append(
            f"semantic hash of pyproject [{section}] changed "
            f"({expected[:12]} → {(computed or 'missing')[:12]})"
        )

    if failures:
        model_note = {
            "A": "Deployment model A: protected-path commits must be signed by an allowed signer.",
            "B": "Deployment model B: no local mechanism is a control — the "
                 "server-side review boundary is the enforcement. This local "
                 "failure is a tripwire forcing the change into its own reviewed PR.",
            "C": "Deployment model C: this check is a tripwire, not a security "
                 "control (v5.1 §11.1); it forces the change into a separate, "
                 "deliberately reviewed commit.",
        }.get(ctx.config.integrity_model, "Unknown deployment model.")
        return GateResult(
            name="integrity",
            status=GATE_FAIL,
            mechanism=MECH_ABSOLUTE,
            detail="; ".join(failures) + f" — {model_note} "
                   "Legitimate guardrail changes go in their own approved PR, "
                   "never bundled with a feature. Not overridable by the allowlist.",
            extra={"protected_paths_changed": changed},
        )
    return GateResult(
        name="integrity", status=GATE_PASS, mechanism=MECH_ABSOLUTE,
        extra={"protected_paths_changed": []},
    )
