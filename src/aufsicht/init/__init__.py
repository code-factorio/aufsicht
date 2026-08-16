"""Layer 2 — `aufsicht init` (distribution spec §5).

detect → probe → propose → write → verify. Deterministic bootstrap:
probes, writes config, verifies, refuses. `--dry-run` stops after
propose and is the default when stdout is not a TTY, so an agent
invoking init blind gets a plan rather than a mutation.
"""
