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
