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


def _principals(signers_path: Path) -> list[str]:
    """Principals named in an allowed-signers file (first field of each
    non-comment, non-empty line)."""
    principals: list[str] = []
    try:
        for line in signers_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            first = line.split()[0]
            if first not in principals:
                principals.append(first)
    except OSError:
        pass
    return principals


def _extract_ssh_signature(repo: Path, sha: str) -> tuple[bytes, bytes] | None:
    """(armored_signature, payload) for an SSH-signed commit, or None.

    The payload is the commit object with the gpgsig header removed —
    exactly the bytes `ssh-keygen -Y verify` checks the signature
    against; the signature stays in its armored form because that is
    what `-s` consumes. Unsigned commits return None.
    """
    proc = subprocess.run(
        ["git", "cat-file", "commit", sha],
        cwd=str(repo), capture_output=True, check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        return None
    raw = proc.stdout
    lines = raw.split(b"\n")
    headers: list[bytes] = []
    sig_lines: list[bytes] = []
    in_sig = False
    idx = 0
    for idx, line in enumerate(lines):
        if line == b"":
            break  # end of header block
        if line.startswith(b"gpgsig ") or line.startswith(b"gpgsigssh "):
            in_sig = True
            sig_lines.append(line.split(b" ", 1)[1])
            continue
        if in_sig and line.startswith(b" "):
            sig_lines.append(line[1:])
            continue
        in_sig = False
        headers.append(line)
    if not sig_lines:
        return None
    payload = b"\n".join(headers) + b"\n" + b"\n".join(lines[idx:])
    # Re-arm with 70-column base64 lines — the format `ssh-keygen -Y
    # verify -s` consumes (measured: raw decoded bytes are rejected
    # with sshsig_armor: invalid format).
    import base64

    try:
        body = b"".join(l for l in sig_lines if not l.startswith(b"-----"))
        decoded = base64.b64decode(body)
    except Exception:  # noqa: BLE001 — malformed sig is just unsigned
        return None
    b64 = base64.b64encode(decoded)
    armored = b"-----BEGIN SSH SIGNATURE-----\n" + b"\n".join(
        b64[i : i + 70] for i in range(0, len(b64), 70)
    ) + b"\n-----END SSH SIGNATURE-----\n"
    return armored, payload


def _verify_commit_signature(
    repo: Path, sha: str, signers: Path
) -> tuple[bool, str]:
    """Real verification: `ssh-keygen -Y verify` against the allowed-
    signers file, over the actual signature blob and payload (v5.1
    §11.1 model A). Tries every principal in the file — the file IS the
    authorised-principal list."""
    import tempfile

    extracted = _extract_ssh_signature(repo, sha)
    if extracted is None:
        return False, f"commit {sha[:12]} is not SSH-signed"
    signature, payload = extracted
    with tempfile.TemporaryDirectory() as tmp:
        sig_file = Path(tmp) / "sig"
        sig_file.write_bytes(signature)
        for principal in _principals(signers):
            proc = subprocess.run(
                ["ssh-keygen", "-Y", "verify", "-f", str(signers),
                 "-I", principal, "-n", "git", "-s", str(sig_file)],
                input=payload, capture_output=True,
            )
            if proc.returncode == 0:
                return True, f"verified against principal {principal!r}"
    return False, (
        f"signature on {sha[:12]} did not verify against any principal in "
        f"{signers.name}"
    )


def _signers_ok(ctx: GateContext, commits: list[str]) -> tuple[bool, str]:
    """Each commit that touched protected paths must be SSH-signed by a
    principal in the allowed-signers file (deployment model A)."""
    signers = ctx.repo / str(ctx.config.allowed_signers or ".quality/allowed_signers")
    if not signers.is_file():
        return False, f"allowed-signers file {signers} not found"
    for sha in commits:
        ok, why = _verify_commit_signature(ctx.repo, sha, signers)
        if not ok:
            return False, why
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
    verified_note: str | None = None
    if changed:
        if ctx.config.integrity_model == "A":
            commits = _commits_touching(ctx, changed)
            ok, why = _signers_ok(ctx, commits) if commits else (True, "")
            if ok:
                # §11.1 model A: a signed commit verified against the
                # allowed-signers file IS the approval.
                verified_note = why or "verified"
            else:
                failures.append(f"protected paths changed without §11.1 approval ({why}): " + ", ".join(changed))
        else:
            failures.append(
                "protected paths changed: " + ", ".join(changed)
            )

    # Allowlist validation and expiry (v5.1 §10): absolute, not
    # overridable by the allowlist itself.
    import datetime as dt

    from . import allowlist as allowlist_mod

    al = allowlist_mod.load_allowlist(ctx.repo)
    today = dt.date.today()
    ctx.report.allowlist_expiring_within_30d = al.expiring_within_30d(today)
    for problem in allowlist_mod.validate(al.entries, today):
        failures.append(f"allowlist validation: {problem}")
    for e in allowlist_mod.expired_entries(al.entries, today):
        failures.append(
            f"allowlist entry expired {e.expires}: {e.rule} ({e.path or 'no path'}) — "
            "no silent permanence (v5.1 §10)"
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
        extra={
            "protected_paths_changed": [],
            **({"signature_verification": verified_note} if verified_note else {}),
        },
    )
