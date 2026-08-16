---
name: aufsicht
description: >-
  Install deterministic AI code quality guardrails into an existing
  Python repository — when the user asks to add quality gates, CI
  quality checks, regression guardrails, or "aufsicht" to a repo, or
  wants AI-agent-proof quality enforcement set up.
---

# aufsicht — installing the guardrails

You are delivering a versioned, deterministic guardrail system, not
improvising one. Everything with teeth lives in the pinned `aufsicht`
runner and in `.quality/` inside the repository. Your job is running
the installer and translating its output — nothing else.

## When to invoke

- The user wants quality guardrails, a quality gate, or CI checks
  guarding against AI-introduced regressions in a Python repository.
- The repository is a git repository with a supported package manager
  (uv or pip).

## When NOT to invoke

- The repository already has `.quality/` — it is already guarded; say
  so and stop. Re-running the installer over an existing installation
  resets policy and requires an explicit, human-approved `--force`.
- The user asks you to loosen, tune or re-derive the guardrails
  themselves — that is a guardrail-change PR in `.quality/`, reviewed
  by a human, not a skill action.
- Not a git repository, or not Python.

## The procedure

1. Run the bootstrap script from the repository root:

   ```bash
   bash <(curl -fsSL <distribution-url>/install/bootstrap.sh)
   ```

   or, from a checkout of the aufsicht repository,
   `bash install/bootstrap.sh`. It verifies prerequisites, installs the
   pinned runner as an isolated tool, and hands off to `aufsicht init`.
   Never reconstruct what the bootstrap does by hand — run the file.

2. `aufsicht init` prints a plan first (it defaults to dry-run when
   stdout is not a terminal, which is your situation). Read the plan
   and explain it to the user in words before applying anything.

3. How to read the plan:
   - **detected** — what the installer found: package manager, layout,
     test runner, task runner, CI provider, default branch.
   - **probe decisions** — sentences, one per measurement the installer
     took. Relay these verbatim; they are the record of what was
     measured and what was narrowed as a result.
   - **day_one_allowlist** — exceptions the installer proposes, with
     counts. They are written only with the plan's approval and only
     inside the installation PR.
   - **guarantees** — what the installer promises not to touch (the
     project's dependency graph, the absence of a baseline file, the
     default branch).

4. If the plan reports a refusal, relay the remedy to the user and
   stop. Refusals name the exact fix (for example, quality
   configuration that must move out of `pyproject.toml` first). Do not
   work around a refusal.

5. Ask the human about the project's test budget before applying the
   plan if the probes measured a slow suite — whether the fast loop
   should include tests is the user's call, and the plan records the
   measured numbers to base it on.

6. Apply with `aufsicht init --write`. It creates a branch (never the
   default branch), writes configuration only, and commits.

7. Open a PR from that branch. A human merges it — that merge is the
   approval act for every protected path the installation touched.

8. Wiring commands into the task runner: the installed commands are
   `aufsicht fast`, `aufsicht full`, `aufsicht fix`. If the repository
   has a task runner (make / just / poe), offer to add entries named
   `quality-fast`, `quality-full`, `quality-fix` that call them, in the
   runner's own file — that is a normal, reviewable repo change, not a
   guardrail change.

## Notes

- `AGENTS.md` gets one appended, delimited section. Never edit outside
  the delimiters.
- The bootstrap installs a pinned runner version; upgrades go through
  `aufsicht upgrade`, which prints a diff and writes nothing.
- Be loud about what changed and cheap to read as a diff, rather than
  smooth.
