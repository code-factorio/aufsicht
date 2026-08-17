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
    spec_version: str | None = None
    addendum_version: str | None = None

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
        spec_version=data.get("spec_version"),
        addendum_version=data.get("addendum_version"),
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


def _env_complete(env: Path, tools: list[str]) -> bool:
    """A trusted env has its marker AND every expected entry point
    whose shebang interpreter exists.

    Marker-only trust is not enough under concurrent builds, and
    existence-only trust is not enough because entry-point shebangs
    embed absolute interpreter paths — an env relocated after install
    has live-looking scripts that fail with ENOENT. Self-heal instead
    of trusting.
    """
    if not (env / ".aufsicht-ok").is_file():
        return False
    bin_dir = _bin_dir(env)
    for tool in tools:
        exe = bin_dir / tool
        if not exe.exists():
            return False
        try:
            first = exe.read_bytes()[:256]
        except OSError:
            return False
        if first.startswith(b"#!"):
            shebang = first.split(b"\n", 1)[0][2:].decode("utf-8", "replace").strip()
            # shebang may carry flags: use the first path-looking token
            for part in shebang.split():
                if "/" in part and not Path(part).exists():
                    return False
    return True


def _build_env_in_place(
    env: Path,
    tools: list[str],
    build,
    *,
    lock_stale_seconds: float = 1800.0,
    wait_timeout: float = 3600.0,
) -> Path:
    """Build *env* in place (never rename a venv: entry-point shebangs
    embed absolute paths), guarded by a lock so concurrent processes
    don't interleave installs."""
    import time as _time

    if _env_complete(env, tools):
        return env
    env.parent.mkdir(parents=True, exist_ok=True)
    lock = env.with_name(env.name + ".lock")
    if _env_complete(env, tools):
        return env
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        deadline = _time.monotonic() + wait_timeout
        while _time.monotonic() < deadline:
            if _env_complete(env, tools):
                return env
            try:
                if _time.time() - lock.stat().st_mtime > lock_stale_seconds:
                    lock.unlink()  # builder died; take over
            except OSError:
                pass
            _time.sleep(1.0)
        raise ToolingError(
            f"timed out waiting for a concurrent build of {env.name}",
            remedy="Remove stale caches under the aufsicht cache dir and retry.",
        )
    try:
        if not _env_complete(env, tools):
            if env.exists():
                shutil.rmtree(env)
            build(env)
            (env / ".aufsicht-ok").write_text("built-by-aufsicht\n", encoding="utf-8")
            if not _env_complete(env, tools):
                missing = [t for t in tools if not (_bin_dir(env) / t).exists()]
                detail = (
                    f"missing entry points for {missing}"
                    if missing
                    else "entry-point shebangs are broken (interpreter path does not exist)"
                )
                raise ToolingError(
                    f"environment {env.name} incomplete after build ({detail})",
                    remedy="Check the pinned versions and network access.",
                )
    finally:
        try:
            lock.unlink()
        except OSError:
            pass
    return env


# Pinned packages that ship no console script (pytest plugins are
# imported, not executed): they cannot be verified via bin/<name>, so
# they are excluded from the entry-point completeness check.
PLUGIN_ONLY_TOOLS = frozenset({"pytest-cov"})


def _ensure_env(
    root: Path, key: str, pins: list[str], *, python: str | None = None
) -> Path:
    """Return a ready env at *root/key*, building it once."""
    entry_points = [p.split("==")[0] for p in pins if p.split("==")[0] not in PLUGIN_ONLY_TOOLS]
    return _build_env_in_place(
        root / key, entry_points, lambda env: _create_env(env, pins, python=python)
    )


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


def project_env_pins(lock: Toolchain) -> tuple[str, ...]:
    """The pins installed into every project env: the test runner and
    its coverage plugin (v5.1 §19: pytest with branch coverage in the
    gate). Order is part of the install command, not the identity."""
    pins: list[str] = []
    if lock.pin("pytest"):
        pins.append(f"pytest=={lock.pin('pytest')}")
    else:
        pins.append("pytest")
    if lock.pin("pytest-cov"):
        pins.append(f"pytest-cov=={lock.pin('pytest-cov')}")
    return tuple(pins)


def project_env(repo: Path, lock: Toolchain, cache: Path | None = None) -> Path:
    """Project dependency environment for the checkout at *repo*.

    Contains the project's third-party dependencies (resolved from this
    checkout's own lockfile/pyproject) plus the pinned pytest and
    coverage plugin — never the project code itself, which changes per
    commit and is made importable via the pytest config's pythonpath
    instead. The cache key includes the env's own pins so a pin bump
    rebuilds rather than reusing an env that lacks the new plugin.
    """
    cache = cache or cache_dir()
    files_key, _lockfile = project_env_key(repo)
    root = cache / "projenvs"
    root.mkdir(parents=True, exist_ok=True)
    pins = project_env_pins(lock)
    pin_key = hashlib.sha256("\n".join(pins).encode()).hexdigest()[:12]
    entry_points = [
        p.split("==")[0] for p in pins if p.split("==")[0] not in PLUGIN_ONLY_TOOLS
    ]

    def build(env: Path) -> None:
        if _have_uv():
            _run(["uv", "venv", "--quiet", str(env)])
            install = [
                "uv", "pip", "install", "--quiet",
                "--python", str(_bin_dir(env) / "python"),
                "-r", "pyproject.toml", *pins,
            ]
            proc = subprocess.run(install, cwd=str(repo), capture_output=True, text=True, timeout=900)
            if proc.returncode != 0:
                # A project may have no dependencies at all; fall back to
                # the pins only rather than failing the whole gate.
                _run(install[: -len(pins)] + list(pins))
        else:
            base = shutil.which("python3") or shutil.which("python")
            if base is None:
                raise ToolingError("neither uv nor python3 is available to build environments")
            _run([base, "-m", "venv", str(env)])
            _run([str(_bin_dir(env) / "python"), "-m", "pip", "install",
                  "--quiet", "--disable-pip-version-check", *pins])

    return _build_env_in_place(
        root / f"proj-{files_key}-{pin_key}", entry_points, build
    )


def project_lockfile_differs(base_repo: Path, head_repo: Path) -> bool:
    """True when the project dependency resolution differs between the
    two checkouts (v5.1 §4.4: exempt Pyright and deptry, flag the run).
    Compares the project files hash only — NOT the env's tool pins, so
    a toolchain bump does not masquerade as a dependency-environment
    change."""
    return project_env_key(base_repo)[0] != project_env_key(head_repo)[0]
