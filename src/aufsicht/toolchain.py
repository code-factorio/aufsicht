"""Analyzer environments from `.quality/toolchain.lock` (v5.1 §4.4).

Two distinct environments, held constant or varied deliberately:

    Analyzer environment       SAME for both runs. Exact versions from
                               HEAD's .quality/toolchain.lock. The BASE
                               worktree MUST NOT influence analyzer
                               selection.
    Project dependencies       Per commit. BASE source resolves from
                               BASE's project lockfile, HEAD source from
                               HEAD's, where the analyzer needs them.

Environments are cached keyed on the hash of what defines them — the
lockfile bytes for analyzers, the project lockfile/pyproject bytes for
project deps — and created outside the repository so the guarded repo's
dependency graph is untouched (distribution spec §3).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import TOOLCHAIN_SCHEMA_VERSION
from .config import cache_dir
from .errors import ToolingError

TOOLCHAIN_PATH = ".quality/toolchain.lock"

# Tools whose findings depend on the project dependency environment
# (v5.1 §4.4): Pyright resolves imports against it, deptry's entire
# subject is it.
ENVIRONMENT_SENSITIVE_TOOLS = frozenset({"pyright", "deptry"})

# Files that define a project's dependency resolution, in priority
# order. The first match is the cache key; the pyproject is included in
# the hash either way so a dependency edit without a lock still
# invalidates the cached environment.
PROJECT_LOCKFILES = (
    "uv.lock",
    "poetry.lock",
    "pdm.lock",
    "requirements.txt",
    "requirements-dev.txt",
)


@dataclass(frozen=True)
class Toolchain:
    tools: dict[str, str]
    runner_version: str | None
    raw_hash: str  # sha256 of the file bytes — the analyzer-env cache key

    def pin(self, tool: str) -> str | None:
        return self.tools.get(tool)


def load_toolchain(repo: Path) -> Toolchain:
    path = repo / TOOLCHAIN_PATH
    if not path.is_file():
        raise ToolingError(
            f"{TOOLCHAIN_PATH} not found",
            remedy="Run `aufsicht init` to pin the analyzer toolchain "
                   "(v5.1 §4.4: a ratchet MUST NOT compare diagnostics "
                   "produced by different versions of the same analyzer).",
        )
    import tomllib

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise ToolingError(f"cannot parse {TOOLCHAIN_PATH}: {exc}") from exc

    version = data.get("schema_version")
    if version != TOOLCHAIN_SCHEMA_VERSION:
        raise ToolingError(
            f"{TOOLCHAIN_PATH}: schema_version must be {TOOLCHAIN_SCHEMA_VERSION}, got {version!r}",
            remedy="Regenerate the toolchain lock with `aufsicht init --force` "
                   "or `aufsicht upgrade`.",
        )

    tools_table = data.get("tools", {})
    if not isinstance(tools_table, dict) or not tools_table:
        raise ToolingError(
            f"{TOOLCHAIN_PATH}: no [tools] pins found",
            remedy="Pin exact versions (never ranges) for every quality tool.",
        )
    tools: dict[str, str] = {}
    for name, pin in tools_table.items():
        if not isinstance(pin, str) or not _is_exact_pin(pin):
            raise ToolingError(
                f"{TOOLCHAIN_PATH}: pin for {name!r} must be an exact version, got {pin!r}",
                remedy="Exact versions, not ranges — a range is a drifting "
                       "instrument that looks pinned (v5.1 §4.4).",
            )
        tools[name] = pin

    runner_version = data.get("runner_version")
    if runner_version is not None and not isinstance(runner_version, str):
        raise ToolingError(f"{TOOLCHAIN_PATH}: runner_version must be a string")

    return Toolchain(
        tools=tools,
        runner_version=runner_version,
        raw_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _bin_dir(env: Path) -> Path:
    return env / ("Scripts" if os.name == "nt" else "bin")


def _is_exact_pin(pin: str) -> bool:
    """True for `1.2.3` / `0.16.3` / `1.1.411` — false for any range,
    wildcard or local-path spec (v5.1 §4.4)."""
    return bool(_EXACT_PIN.match(pin))


_EXACT_PIN = re.compile(r"^\d+(\.\d+)+$")


def tool_exe(env: Path, tool: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    return _bin_dir(env) / f"{tool}{suffix}"


def _have_uv() -> bool:
    return shutil.which("uv") is not None


def _run(cmd: list[str], *, timeout: int = 900) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise ToolingError(
            f"environment setup failed: {' '.join(cmd)}\n"
            f"{proc.stderr.strip()[:2000] or proc.stdout.strip()[:2000]}",
            remedy="Check network access and the pinned versions in "
                   f"{TOOLCHAIN_PATH}.",
        )


def _create_env(target: Path, pins: list[str], *, python: str | None) -> None:
    """Create a venv at *target* and install exact *pins*."""
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if _have_uv():
        cmd = ["uv", "venv", "--quiet", str(target)]
        if python:
            cmd += ["--python", python]
        _run(cmd)
        _run(
            ["uv", "pip", "install", "--quiet", "--python", str(_bin_dir(target) / "python")]
            + pins
        )
    else:
        base = shutil.which("python3") or shutil.which("python")
        if base is None:
            raise ToolingError("neither uv nor python3 is available to build environments")
        # python -m venv has no --python; the fallback relies on the
        # ambient interpreter (uv is the preferred path).
        _run([base, "-m", "venv", str(target)])
        _run([str(_bin_dir(target) / "python"), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", *pins])


def _ensure_env(
    root: Path, key: str, pins: list[str], *, python: str | None = None
) -> Path:
    """Return a ready env at *root/key*, building it atomically once."""
    env = root / key
    marker = env / ".aufsicht-ok"
    if marker.is_file():
        return env
    tmp = root / f"{key}.tmp.{os.getpid()}"
    if tmp.exists():
        shutil.rmtree(tmp)
    try:
        _create_env(tmp, pins, python=python)
        (tmp / ".aufsicht-ok").write_text("\n".join(pins), encoding="utf-8")
        try:
            tmp.rename(env)  # atomic on the same filesystem
        except OSError:
            # Lost a race, or it already exists: use the winner.
            if marker.is_file():
                shutil.rmtree(tmp, ignore_errors=True)
                return env
            raise
    except ToolingError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return env


def analyzer_env(lock: Toolchain, cache: Path | None = None) -> Path:
    """The analyzer environment: exact versions from HEAD's lock.

    One environment, shared by the BASE and HEAD runs — the BASE
    worktree never chooses analyzer versions (v5.1 §4.4). Python 3.12 is
    pinned for analyzer compatibility regardless of the ambient
    interpreter.
    """
    cache = cache or cache_dir()
    root = cache / "envs"
    root.mkdir(parents=True, exist_ok=True)
    key = "analyzer-" + lock.raw_hash[:24]
    return _ensure_env(root, key, [f"{n}=={v}" for n, v in sorted(lock.tools.items())], python="3.12")


def project_env_key(repo: Path) -> tuple[str, str]:
    """Cache key material for a project dependency environment.

    Returns (key, lockfile_name_or_"pyproject"). Hashed over the
    project lockfile when one exists and the pyproject dependency
    sections — a dependency edit without a lockfile still invalidates
    the cached environment.
    """
    import tomllib

    h = hashlib.sha256()
    lockfile = next((n for n in PROJECT_LOCKFILES if (repo / n).is_file()), "pyproject")
    h.update(lockfile.encode())
    if lockfile != "pyproject":
        h.update((repo / lockfile).read_bytes())
    pyproject = repo / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            data = {}
        project = data.get("project", {}) if isinstance(data.get("project"), dict) else {}
        h.update(json.dumps(
            {
                "deps": project.get("dependencies", []),
                "optional_deps": project.get("optional-dependencies", {}),
                "groups": project.get("dependency-groups", {}),
                "requires_python": project.get("requires-python", ""),
            },
            sort_keys=True,
        ).encode())
    return h.hexdigest()[:24], lockfile


def project_env(repo: Path, lock: Toolchain, cache: Path | None = None) -> Path:
    """Project dependency environment for the checkout at *repo*.

    Contains the project's third-party dependencies (resolved from this
    checkout's own lockfile/pyproject) plus the pinned pytest — never
    the project code itself, which changes per commit and is made
    importable via the pytest config's pythonpath instead.
    """
    cache = cache or cache_dir()
    key, _lockfile = project_env_key(repo)
    root = cache / "projenvs"
    root.mkdir(parents=True, exist_ok=True)
    pytest_pin = f"pytest=={lock.pin('pytest')}" if lock.pin("pytest") else "pytest"
    env = root / f"proj-{key}"
    marker = env / ".aufsicht-ok"
    if marker.is_file():
        return env

    tmp = root / f"proj-{key}.tmp.{os.getpid()}"
    if tmp.exists():
        shutil.rmtree(tmp)
    try:
        if _have_uv():
            _run(["uv", "venv", "--quiet", str(tmp)])
            install = [
                "uv", "pip", "install", "--quiet",
                "--python", str(_bin_dir(tmp) / "python"),
                "-r", "pyproject.toml", pytest_pin,
            ]
            proc = subprocess.run(install, cwd=str(repo), capture_output=True, text=True, timeout=900)
            if proc.returncode != 0:
                # A project may have no dependencies at all; fall back to
                # pytest only rather than failing the whole gate.
                _run(install[:-2])  # pytest pin only
        else:
            base = shutil.which("python3") or shutil.which("python")
            if base is None:
                raise ToolingError("neither uv nor python3 is available to build environments")
            _run([base, "-m", "venv", str(tmp)])
            _run([str(_bin_dir(tmp) / "python"), "-m", "pip", "install",
                  "--quiet", "--disable-pip-version-check", pytest_pin])
        (tmp / ".aufsicht-ok").write_text(pytest_pin, encoding="utf-8")
        try:
            tmp.rename(env)
        except OSError:
            if marker.is_file():
                shutil.rmtree(tmp, ignore_errors=True)
                return env
            raise
    except ToolingError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return env


def project_lockfile_differs(base_repo: Path, head_repo: Path) -> bool:
    """True when the project dependency resolution differs between the
    two checkouts (v5.1 §4.4: exempt Pyright and deptry, flag the run)."""
    return project_env_key(base_repo) != project_env_key(head_repo)
