# aufsicht

Deterministic, reusable quality guardrails for existing Python
repositories, built so AI coding agents cannot silently degrade them.

`aufsicht` (German for *oversight* / *supervision* — the only
non-English identifier in the system) implements the
**Python AI Code Quality Guardrails specification v5.1 (Tier 1)** and
the distribution layer from the **aufsicht distribution spec v1**.
Both documents, plus the Tier 3 mutation-testing addendum, live
verbatim in [`spec/`](spec/).

## The three layers

```text
Layer 1   runner        this package. All gate logic. Versioned, pinned, never
                        regenerated per repository.
Layer 2   init          `aufsicht init` — deterministic bootstrap: probes,
                        writes .quality/, verifies, refuses.
Layer 3   skill         delivery via an agent harness. Zero gate logic, zero
                        policy. Runs install/bootstrap.sh and reads init's
                        output. That is all.
```

The invariant: **no policy above Layer 1**. Thresholds, rule lists and
mechanisms live in the runner and in `.quality/` inside each guarded
repository — never in the skill, never in prose an agent paraphrases.

## Install

```bash
uv tool install aufsicht==0.2.0        # or: pipx install aufsicht==0.2.0
cd /path/to/existing/repo
aufsicht init                          # detect → probe → propose → write → verify
```

`init` branches (never commits to the default branch), writes `.quality/`,
a CI workflow, an AGENTS.md section — configuration and nothing else.
The repository's `pyproject.toml` dependencies and lockfile are
provably untouched: the runner is a standalone tool, not a project
dependency (distribution spec §3).

## Command surface

```text
aufsicht fast      quality-fast   read-only, diff-scoped, < 15 s (v5.1 §5)
aufsicht full      quality-full   read-only, whole repo, per-rule ratchets
aufsicht fix       quality-fix    MAY mutate — never in validation or CI
aufsicht init      Layer 2 installer
aufsicht upgrade   prints the .quality/ diff a runner upgrade would apply,
                   writes nothing
```

Exit codes (v5.1 §15): `0` pass, `1` hard gate, `2` regression only,
`3` tooling error. Init exit codes (distribution spec §5.2): `0`
installed, `1` refused, `2` installed with warnings, `3` tooling error.

## What the gate does

Tier 1 — no baseline artifact, three mechanisms (v5.1 §4):

* **absolute** — test failures, circular imports (pyscn structured
  analysis minus allowlist), pip-audit findings, guardrail integrity
* **diff-scoped** — Ruff errors/format, enumerated Ruff `S` rules,
  C901 complexity (changed-file scope — measured, see
  `src/aufsicht/probe_facts.py`), Pyright on changed files,
  Semgrep anti-evasion rules, suppression-comment added-line scan
* **per-rule count ratchet** — Ruff, Pyright, pyscn dead code, deptry,
  Xenon aggregate, grouped by rule id with a `<no-rule>` bucket for
  null rule ids

Analyzer versions come from `.quality/toolchain.lock` (protected
path, v5.1 §11.2) and run from runner-managed environments keyed on
lockfile hash. A ratchet never compares diagnostics produced by
different versions of the same analyzer (v5.1 §4.4).

## Self-hosting

This repository runs its own guardrails (`.quality/`,
`.github/workflows/aufsicht.yml`). `src/aufsicht/**` is documented in
AGENTS.md as the product rather than a protected path — its control is
code review plus the self-test suite (distribution spec §7).

## Development

```bash
uv sync
uv run pytest
```

The self-test suite (v5.1 §18, distribution spec §8) executes every
case against generated scratch repositories — real git history, real
merge base, real violation, real CLI invocation.

## License

Apache-2.0
