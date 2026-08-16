"""Load `.quality/config.toml` (distribution spec §12 step 1).

All thresholds configurable in `.quality/config.toml` (v5.1 §8); the
defaults here exist so the spec is implementable without further
negotiation. The file carries an integer ``schema_version`` so a runner
can refuse — loudly — configuration from a newer schema rather than
silently misreading it.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import CONFIG_SCHEMA_VERSION
from .errors import ConfigError

CONFIG_DIR = ".quality"
CONFIG_PATH = f"{CONFIG_DIR}/config.toml"

# v5.1 §9.1: the observable-verification act set, configurable, shipped
# with these defaults.
DEFAULT_VERIFICATION_STATEMENTS = ["assert"]
DEFAULT_VERIFICATION_CALLS = [
    "pytest.raises", "pytest.warns", "pytest.deprecated_call",
    "self.assert*", "self.fail",
    "assert_*", "check_*", "verify_*",
    "*.assert_called*", "*.assert_has_calls",
    "snapshot", "verify",
]


# CI base-ref variables, tried in order (v5.1 §4.6 step 1). Which one
# carries the base differs per provider; the first that is set wins.
CI_DEFAULTS = (
    "GITHUB_BASE_SHA",
    "GITHUB_BASE_REF",
    "CI_MERGE_REQUEST_DIFF_BASE_SHA",
    "CI_MERGE_REQUEST_TARGET_BRANCH_NAME",
)


@dataclass(frozen=True)
class QualityConfig:
    schema_version: int
    base_ref: str | None = None
    base_ci_env: tuple[str, ...] = CI_DEFAULTS
    fast_budget_seconds: float = 15.0
    fast_pyright: str = "changed-files"   # or "off" (probe-narrowed, exit 2 at init)
    fast_pytest: str = "affected"         # or "off"
    fast_semgrep: str = "changed-files"
    tests_budget_seconds: int | None = None
    tests_runner_args: tuple[str, ...] = ()
    c901_max: int = 10
    xenon_max_average: str = "A"
    xenon_max_modules: str = "A"
    xenon_max_absolute: str = "C"
    verification_statements: tuple[str, ...] = tuple(DEFAULT_VERIFICATION_STATEMENTS)
    verification_calls: tuple[str, ...] = tuple(DEFAULT_VERIFICATION_CALLS)
    integrity_model: str = "B"            # v5.1 §11.1 deployment model A/B/C
    allowed_signers: str | None = None    # required for model A
    extra_protected: tuple[str, ...] = ()
    pip_audit_enabled: bool = True
    disabled_gates: tuple[str, ...] = ()
    mutation_enabled: bool = False        # Tier 3 — parsed, gated off
    mutation_floor: float = 0.60
    mutation_min_scoreable: int = 20
    mutation_function_cap: int = 25
    mutation_wall_clock_minutes: int = 20
    raw: dict = field(default_factory=dict)

    @classmethod
    def load(cls, repo: Path) -> "QualityConfig":
        path = repo / CONFIG_PATH
        if not path.is_file():
            raise ConfigError(
                f"{CONFIG_PATH} not found",
                remedy="Run `aufsicht init` to generate the guardrail "
                       "configuration, or cd into the repository root.",
            )
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError) as exc:
            raise ConfigError(f"cannot parse {CONFIG_PATH}: {exc}") from exc
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> "QualityConfig":
        version = data.get("schema_version")
        if not isinstance(version, int):
            raise ConfigError(
                f"{CONFIG_PATH}: schema_version must be an integer",
                remedy="Add `schema_version = {CONFIG_SCHEMA_VERSION}` at the top.",
            )
        if version > CONFIG_SCHEMA_VERSION:
            raise ConfigError(
                f"config schema_version {version} is newer than this runner "
                f"understands ({CONFIG_SCHEMA_VERSION})",
                remedy="Upgrade the aufsicht runner to match the repository's "
                       "configuration (distribution spec §10).",
            )
        if version < CONFIG_SCHEMA_VERSION:
            raise ConfigError(
                f"config schema_version {version} is older than this runner "
                f"understands ({CONFIG_SCHEMA_VERSION})",
                remedy="Run `aufsicht upgrade` and review the proposed diff.",
            )

        base = _table(data, "base")
        fast = _table(data, "fast")
        tests = _table(data, "tests")
        complexity = _table(data, "complexity")
        verification = _table(data, "test_verification")
        integrity = _table(data, "integrity")
        pip_audit = _table(data, "pip_audit")
        gates = _table(data, "gates")
        mutation = _table(data, "mutation")

        return cls(
            schema_version=version,
            base_ref=_opt_str(base, "ref"),
            base_ci_env=tuple(base.get("ci_env", ())) or CI_DEFAULTS,
            fast_budget_seconds=float(fast.get("budget_seconds", 15.0)),
            fast_pyright=str(fast.get("pyright", "changed-files")),
            fast_pytest=str(fast.get("pytest", "affected")),
            fast_semgrep=str(fast.get("semgrep", "changed-files")),
            tests_budget_seconds=(
                int(tests["budget_seconds"]) if "budget_seconds" in tests else None
            ),
            tests_runner_args=tuple(str(a) for a in tests.get("runner_args", ())),
            c901_max=int(complexity.get("c901_max", 10)),
            xenon_max_average=str(complexity.get("xenon_max_average", "A")),
            xenon_max_modules=str(complexity.get("xenon_max_modules", "A")),
            xenon_max_absolute=str(complexity.get("xenon_max_absolute", "C")),
            verification_statements=tuple(
                verification.get("statements", DEFAULT_VERIFICATION_STATEMENTS)
            ),
            verification_calls=tuple(
                verification.get("calls", DEFAULT_VERIFICATION_CALLS)
            ),
            integrity_model=str(integrity.get("deployment_model", "B")),
            allowed_signers=_opt_str(integrity, "allowed_signers"),
            extra_protected=tuple(str(p) for p in integrity.get("extra_protected", ())),
            pip_audit_enabled=bool(pip_audit.get("enabled", True)),
            disabled_gates=tuple(str(g) for g in gates.get("disable", ())),
            mutation_enabled=bool(mutation.get("enabled", False)),
            mutation_floor=float(mutation.get("floor", 0.60)),
            mutation_min_scoreable=int(mutation.get("min_scoreable", 20)),
            mutation_function_cap=int(mutation.get("function_cap", 25)),
            mutation_wall_clock_minutes=int(mutation.get("wall_clock_minutes", 20)),
            raw=data,
        )


def _table(data: dict, key: str) -> dict:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{CONFIG_PATH}: [{key}] must be a table")
    return value


def _opt_str(table: dict, key: str) -> str | None:
    value = table.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{CONFIG_PATH}: {key} must be a string")
    return value


def cache_dir() -> Path:
    """Runtime cache root — envs, worktrees, ratchet counts.

    Never inside the repository: base counts are computed, never
    committed (v5.1 §4.3), and analyzer environments must not leak into
    the project's dependency graph (distribution spec §3).
    """
    override = os.environ.get("AUFSICHT_CACHE_DIR")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg) / "aufsicht"
    return Path.home() / ".cache" / "aufsicht"
