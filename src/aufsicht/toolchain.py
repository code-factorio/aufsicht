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

# Poll interval for a waiter on a concurrent env build (seconds).
_ENV_BUILD_POLL_SECONDS = 1.0

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


def _require_uv() -> None:
    """uv is the supported resolver for project dependency environments
    (issue #19); there is no pins-only fallback — an env without the
    project's dependencies would make the pytest gate report the broken
    environment as test findings. A broken environment surfaces as a
    tooling error (exit 3), never as defective project code."""
    if not _have_uv():
        raise ToolingError(
            "uv is required to build the project dependency environment "
            "for the pytest gate, but it is not on PATH",
            remedy="Install uv (https://docs.astral.sh/uv/) and re-run; "
            "the gate will not run a suite against a half-resolved environment.",
        )


def _run(cmd: list[str], *, timeout: int = 900) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise ToolingError(
            f"environment setup failed: {' '.join(cmd)}\n"
            f"{proc.stderr.strip()[:2000] or proc.stdout.strip()[:2000]}",
            remedy=f"Check network access and the pinned versions in {TOOLCHAIN_PATH}.",
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
            [
                "uv",
                "pip",
                "install",
                "--quiet",
                "--python",
                str(_bin_dir(target) / "python"),
                *pins,
            ]
        )
    else:
        base = shutil.which("python3") or shutil.which("python")
        if base is None:
            raise ToolingError("neither uv nor python3 is available to build environments")
        # python -m venv has no --python; the fallback relies on the
        # ambient interpreter (uv is the preferred path).
        _run([base, "-m", "venv", str(target)])
        _run(
            [
                str(_bin_dir(target) / "python"),
                "-m",
                "pip",
                "install",
                "--quiet",
                "--disable-pip-version-check",
                *pins,
            ]
        )


def _normalize_dist_name(name: str) -> str:
    """PEP 503 normalization: pins use the PyPI form (`pip-audit`),
    dist-info directories the escaped one (`pip_audit`)."""
    return re.sub(r"[-_.]+", "-", name).lower()


# `<escaped name>-<version>.dist-info` — escaped names contain no `-`,
# so the first hyphen separates name from version.
_DIST_INFO_NAME = re.compile(r"^(?P<name>[A-Za-z0-9_.]+)-(?P<version>.+)\.dist-info$")


def _site_packages(env: Path) -> Path | None:
    """The env's site-packages directory, or None when the layout is
    not a venv's (version verification then finds nothing)."""
    if os.name == "nt":
        cand = env / "Lib" / "site-packages"
        return cand if cand.is_dir() else None
    lib = env / "lib"
    if not lib.is_dir():
        return None
    for child in sorted(lib.iterdir()):
        cand = child / "site-packages"
        if child.name.startswith("python") and cand.is_dir():
            return cand
    return None


def _installed_versions(env: Path) -> dict[str, str]:
    """Installed distributions as {normalized name: version}, read from
    dist-info directory names — a directory scan, no subprocess calls."""
    site = _site_packages(env)
    if site is None:
        return {}
    found: dict[str, str] = {}
    for entry in site.iterdir():
        match = _DIST_INFO_NAME.match(entry.name)
        if match is None:
            continue
        name = _normalize_dist_name(match.group("name"))
        version = match.group("version")
        if found.setdefault(name, version) != version:
            # Two distributions of one package: matches no exact pin.
            found[name] = ""
    return found


def _version_mismatches(env: Path, pins: dict[str, str]) -> list[str]:
    """Pinned tools whose installed version differs from the pin (or
    whose distribution is absent), as `name: installed != pinned`."""
    installed = _installed_versions(env)
    return [
        f"{name}: {installed.get(_normalize_dist_name(name))!r} != {want!r}"
        for name, want in sorted(pins.items())
        if installed.get(_normalize_dist_name(name)) != want
    ]


def _env_complete(env: Path, entry_points: list[str], pins: dict[str, str]) -> bool:
    """A trusted env has its marker AND every expected entry point
    whose shebang interpreter exists AND every pinned distribution
    installed at exactly its pinned version.

    Marker-only trust is not enough under concurrent builds, and
    existence-only trust is not enough because entry-point shebangs
    embed absolute interpreter paths — an env relocated after install
    has live-looking scripts that fail with ENOENT. Neither catches an
    env restored from a cache with the marker intact but wrong tool
    versions installed. Verify instead of trusting: a failed
    verification is a rebuild, never a pass and never a silent skip.
    """
    if not (env / ".aufsicht-ok").is_file():
        return False
    bin_dir = _bin_dir(env)
    for tool in entry_points:
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
    return not _version_mismatches(env, pins)


def _incompleteness_detail(env: Path, entry_points: list[str], pins: dict[str, str]) -> str:
    """Why a freshly built env still fails verification."""
    missing = [t for t in entry_points if not (_bin_dir(env) / t).exists()]
    if missing:
        return f"missing entry points for {missing}"
    wrong = _version_mismatches(env, pins)
    if wrong:
        return f"installed versions disagree with the pins: {wrong}"
    return "entry-point shebangs are broken (interpreter path does not exist)"


def _wait_for_concurrent_build(
    env: Path,
    entry_points: list[str],
    pins: dict[str, str],
    lock: Path,
    *,
    lock_stale_seconds: float,
    wait_timeout: float,
) -> bool:
    """Block until the process holding *lock* finishes building *env*.

    Returns True once the env verifies complete. A lock whose mtime is
    older than *lock_stale_seconds* belongs to a dead builder: unlink
    it and return False so the CALLER re-acquires and builds — a lone
    waiter must not keep polling an env nobody is building (observed:
    a stale lock from a killed process cost a full wait_timeout of
    polling before failing). Raises on timeout.
    """
    import time as _time

    deadline = _time.monotonic() + wait_timeout
    while _time.monotonic() < deadline:
        if _env_complete(env, entry_points, pins):
            return True
        try:
            if _time.time() - lock.stat().st_mtime > lock_stale_seconds:
                lock.unlink()  # builder died; hand the lock back
                return False
        except OSError:
            # The lock vanished: the builder finished or died; with the
            # env still incomplete the caller must retry acquisition.
            return False
        _time.sleep(_ENV_BUILD_POLL_SECONDS)
    raise ToolingError(
        f"timed out waiting for a concurrent build of {env.name}",
        remedy="Remove stale caches under the aufsicht cache dir and retry.",
    )


def _build_env_in_place(
    env: Path,
    entry_points: list[str],
    pins: dict[str, str],
    build,
    *,
    lock_stale_seconds: float = 1800.0,
    wait_timeout: float = 3600.0,
) -> Path:
    """Build *env* in place (never rename a venv: entry-point shebangs
    embed absolute paths), guarded by a lock so concurrent processes
    don't interleave installs."""
    env.parent.mkdir(parents=True, exist_ok=True)
    lock = env.with_name(env.name + ".lock")
    while True:
        if _env_complete(env, entry_points, pins):
            return env
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
        except FileExistsError:
            if _wait_for_concurrent_build(
                env,
                entry_points,
                pins,
                lock,
                lock_stale_seconds=lock_stale_seconds,
                wait_timeout=wait_timeout,
            ):
                return env  # the concurrent build verified complete
            continue  # stale/dead lock taken over: acquire and build
        break
    try:
        if not _env_complete(env, entry_points, pins):
            if env.exists():
                shutil.rmtree(env)
            build(env)
            (env / ".aufsicht-ok").write_text("built-by-aufsicht\n", encoding="utf-8")
            if not _env_complete(env, entry_points, pins):
                raise ToolingError(
                    f"environment {env.name} incomplete after build "
                    f"({_incompleteness_detail(env, entry_points, pins)})",
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
# they are excluded from the entry-point completeness check — their
# dist-info version is still verified like every other pin.
PLUGIN_ONLY_TOOLS = frozenset({"pytest-cov", "pytest-xdist"})


def _pin_versions(pins: list[str]) -> dict[str, str]:
    """name → pinned version for pins that carry one (an unpinned
    fallback such as `pytest` has no expected version to compare)."""
    versions: dict[str, str] = {}
    for pin in pins:
        name, sep, version = pin.partition("==")
        if sep:
            versions[name] = version
    return versions


def _entry_points(pins: list[str] | tuple[str, ...]) -> list[str]:
    """Console-script names for *pins* — plugin-only tools ship none
    and are exempt from the entry-point completeness check."""
    return [p.split("==")[0] for p in pins if p.split("==")[0] not in PLUGIN_ONLY_TOOLS]


def _ensure_env(root: Path, key: str, pins: list[str], *, python: str | None = None) -> Path:
    """Return a ready env at *root/key*, building it once."""
    entry_points = _entry_points(pins)
    return _build_env_in_place(
        root / key,
        entry_points,
        _pin_versions(pins),
        lambda env: _create_env(env, pins, python=python),
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
    key = "analyzer-" + lock.raw_hash[:24]
    return _ensure_env(
        root, key, [f"{n}=={v}" for n, v in sorted(lock.tools.items())], python="3.12"
    )


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
        h.update(
            json.dumps(
                {
                    "deps": project.get("dependencies", []),
                    "optional_deps": project.get("optional-dependencies", {}),
                    "groups": project.get("dependency-groups", {}),
                    "requires_python": project.get("requires-python", ""),
                },
                sort_keys=True,
            ).encode()
        )
    return h.hexdigest()[:24], lockfile


def project_env_pins(lock: Toolchain) -> tuple[str, ...]:
    """The pins installed into every project env: the test runner and
    its coverage plugin (v5.1 §19: pytest with branch coverage in the
    gate), plus the optional parallel-runner plugin when the lock pins
    it (CI-speed plan M3: a guardrail PR adds the pin to switch the
    gate suite to xdist; no pin means today's env, exactly). Order is
    part of the install command, not the identity."""
    pins: list[str] = []
    if lock.pin("pytest"):
        pins.append(f"pytest=={lock.pin('pytest')}")
    else:
        pins.append("pytest")
    if lock.pin("pytest-cov"):
        pins.append(f"pytest-cov=={lock.pin('pytest-cov')}")
    if lock.pin("pytest-xdist"):
        pins.append(f"pytest-xdist=={lock.pin('pytest-xdist')}")
    return tuple(pins)


def _has_installable_project(repo: Path) -> bool:
    """True when the checkout carries a pyproject.toml with a [project]
    table — the only shape `uv pip install -r pyproject.toml` resolves.
    Installability is decided from the file, never from a uv failure: a
    failure while the project does declare dependencies is a resolution
    problem that must surface as a tooling error, because a pins-only
    retry verifies complete (only the pins are checked) while holding
    none of the project's dependencies (PR #22 review). An unparseable
    pyproject counts as installable so uv is the one to report it."""
    import tomllib

    pyproject = repo / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return True
    return isinstance(data.get("project"), dict)


def project_env(repo: Path, lock: Toolchain, cache: Path | None = None) -> Path:
    """Project dependency environment for the checkout at *repo*.

    Contains the project's third-party dependencies (resolved from this
    checkout's own lockfile/pyproject) plus the pinned pytest and
    coverage plugin — never the project code itself, which changes per
    commit and is made importable via the pytest config's pythonpath
    instead. The cache key includes the env's own pins so a pin bump
    rebuilds rather than reusing an env that lacks the new plugin.

    Building the env requires uv (issue #19): without it the project
    dependencies cannot be resolved, and the fallback that installed
    only the pins made the pytest gate report the broken environment as
    test findings. A cached-complete env is exempt — it verifies without
    a build, so a warm cache works even without uv. A pyproject that
    declares dependencies uv cannot resolve is likewise a tooling error,
    never a silent pins-only env: only a checkout with nothing
    installable (no pyproject.toml, or one without a [project] table)
    builds on the pins alone — decided from the file, never from a uv
    failure (PR #22 review).
    """
    cache = cache or cache_dir()
    files_key, _lockfile = project_env_key(repo)
    root = cache / "projenvs"
    pins = project_env_pins(lock)
    pin_key = hashlib.sha256("\n".join(pins).encode()).hexdigest()[:12]
    entry_points = _entry_points(pins)

    def build(env: Path) -> None:
        # Required here, not in project_env: _build_env_in_place
        # short-circuits on a complete cached env, so a warm cache works
        # without uv — only a build that is actually needed but
        # impossible fails (issue #19).
        _require_uv()
        _run(["uv", "venv", "--quiet", str(env)])
        base = [
            "uv",
            "pip",
            "install",
            "--quiet",
            "--python",
            str(_bin_dir(env) / "python"),
        ]
        if _has_installable_project(repo):
            install = [*base, "-r", "pyproject.toml", *pins]
            proc = subprocess.run(
                install, cwd=str(repo), capture_output=True, text=True, timeout=900
            )
            if proc.returncode != 0:
                # The project declares dependencies and uv could not
                # resolve them (private index, constraint conflict,
                # build failure). Never degrade to the pins here: that
                # env verifies complete — only the pins are checked —
                # and the pytest gate would report the missing imports
                # as test findings (PR #22 review). A broken environment
                # is a tooling error, exit 3.
                raise ToolingError(
                    f"project dependency install failed: {' '.join(install)}\n"
                    f"{proc.stderr.strip()[:2000] or proc.stdout.strip()[:2000]}",
                    remedy="Fix the resolution failure (index access, version "
                    "constraints, build) — the gate will not run a suite "
                    "against a half-resolved environment.",
                )
        else:
            # Nothing installable: no pyproject.toml, or one without a
            # [project] table (uv fails such a -r install with a
            # metadata error). The pins only — decided from the file,
            # never from a uv failure (issue #19), so the gate still
            # gets its runner without a retry that could mask a real
            # resolution problem.
            _run([*base, *pins])

    return _build_env_in_place(
        root / f"proj-{files_key}-{pin_key}", entry_points, _pin_versions(list(pins)), build
    )


def project_lockfile_differs(base_repo: Path, head_repo: Path) -> bool:
    """True when the project dependency resolution differs between the
    two checkouts (v5.1 §4.4: exempt Pyright and deptry, flag the run).
    Compares the project files hash only — NOT the env's tool pins, so
    a toolchain bump does not masquerade as a dependency-environment
    change."""
    return project_env_key(base_repo)[0] != project_env_key(head_repo)[0]
