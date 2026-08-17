# aufsicht — repository conventions

## Two roles: consumer and evaluator (distribution spec §7)

This repository runs its own guardrails on itself, from the working
tree rather than the published version. `.quality/**` and
`.github/workflows/**` are protected paths as usual (v5.1 §11.2).

`src/aufsicht/**` is **not** a protected path. It is the product — the
evaluator itself. Protecting it under v5.1 §11.2 would mean every
commit to the project trips the integrity check. Its control is code
review plus the self-test suite (`tests/`, executing the v5.1 §18 and
distribution §8 tables against generated scratch repositories), which
is a stronger control than a path rule anyway.

Do not build a threat model on the assumption that `src/aufsicht/**`
is integrity-protected. It is not, by design, and that exception is
recorded here precisely so it does not become an implicit security
assumption.

## Releases: every green push to main publishes (distribution spec §10, §14)

CI publishes to PyPI after a green `quality-full` run. There is no
release day: a merge to main that does not increase the runner version
fails the publish job with instructions. The version lives in five
places — `pyproject.toml`, `src/aufsicht/__init__.py`,
`install/bootstrap.sh`, `skill/scripts/bootstrap.sh` (the byte-identical
pair), `.quality/toolchain.lock` — bump all five in the same commit,
semver per distribution spec §10.

<!-- aufsicht:begin (do not edit outside these delimiters; appended by `aufsicht init`) -->

## Quality guardrails (aufsicht)

Deterministic gates defined by the Python AI Code Quality Guardrails
spec (v5.1). `quality-fast` and `quality-full` come from the pinned
`aufsicht` runner recorded in `.quality/toolchain.lock`.

### Agent workflow

1. Read repository conventions.
2. Understand the change. Inspect existing patterns before writing new ones.
3. Implement the smallest complete solution.
4. Run quality-fast. Fix failures. Repeat until clean.
5. Run quality-full.
6. If a ratchet failed, find and fix what you added. Do not offset it with an
   unrelated fix.
7. Stop. Report status. Do not self-approve.

Never, under any circumstances:

* disable, skip, or xfail a test to make a task complete
* add a suppression comment
* edit anything under the protected paths (`.quality/**`,
  `pyrightconfig.json`, `.pyscn.toml`, `.pre-commit-config.yaml`,
  `.github/workflows/**`, `AGENTS.md`)
* regenerate the baseline
* add or edit an allowlist entry
* weaken a threshold, coverage target, or lint rule
* leave the previous implementation in place after replacing it
* create a parallel implementation where an existing one should be extended
* modify files unrelated to the task

If a gate appears wrong, **stop and report it**. Do not work around it. A task
that requires weakening its own evaluator is a task that needs a human.

### Escape valve

The only sanctioned exception mechanism is `.quality/allowlist.toml`
(v5.1 §10): every entry carries a reason and an expiry. There are no
inline suppression comments and no per-tool ignore files.

<!-- aufsicht:end -->
