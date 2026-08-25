"""aufsicht — deterministic AI code quality guardrails for Python.

Implements spec v5.1 (Tier 1) plus the distribution layer from the
aufsicht distribution spec v1. The runner is Layer 1: all gate logic,
versioned, never regenerated per repository.
"""

__version__ = "0.2.3"

# Spec identity recorded in every report (distribution spec §10).
SPEC_VERSION = "v5.1"
ADDENDUM_VERSION = "final"

# .quality/config.toml schema this runner understands.
CONFIG_SCHEMA_VERSION = 1

# .quality/toolchain.lock schema this runner understands.
TOOLCHAIN_SCHEMA_VERSION = 1
