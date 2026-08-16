# Specification: aufsicht — Distribution (v1 — build spec)

**Status:** draft, intended to be built from. Companion to two frozen documents:

```text
python-ai-guardrails-spec-v5.1.md          what the gate does          (frozen, Tier 1)
tier3-mutation-testing-addendum-FINAL.md   what the Tier 3 gate does   (frozen, later)
this document                              what ships, and how         (build spec)
```

**Precedence.** On gate behaviour — thresholds, mechanisms, scopes, exit codes, report
schema — v5.1 and the addendum win, always. This document never restates a threshold or a
rule list, because a restated number is a second source of truth that will drift. Where this
document mentions a v5.1 concept it cites the section and stops. On packaging, layering,
install contract and repository layout, this document is authoritative and the other two are
silent.

Where prose here disagrees with the acceptance checklist in §13, trust the checklist. That
convention is inherited from v5.1 and it has already earned its keep.

**Cross-reference convention.** Three documents now share one numbering space, so references
are prefixed: `v5.1 §4.6` and `addendum §3.1` point outward, and a bare `§13` is always a
section of this document.

---

## 1. What this closes

v5.1 and the addendum specify a gate to implementation detail and never say what you install.
Read literally, the delivery model is "hand 5000 words of prose to a coding agent, once per
repository." That has three failure modes, and they compound:

1. **Two repositories get two different implementations.** Both green. Neither comparable to
   the other, and a bug found in one is not fixed in the other.
2. **The divergence lands in the worst place.** The parts an agent will paraphrase rather
   than transcribe are base-ref resolution (v5.1 §4.6), cache keying (v5.1 §4.3), and exemption scoping
   (v5.1 §4.5) — precisely the code where "approximately right" and "silently green" produce the
   same output.
3. **There is no upgrade path.** A regenerated implementation has no version, so a fix to the
   ratchet logic cannot be shipped to anything.

The system's premise is determinism. A non-deterministic distribution mechanism for a
determinism tool is the contradiction this document exists to remove.

**The reframe:** the question is not "installer script or coding agent," it is *how many times
does this specification get implemented*. Once, into a versioned artifact, is the answer. A
coding agent is the right tool to build that artifact — one time, against §13 and against the
self-test tables in v5.1 §18 and addendum §9. It is the wrong tool to re-derive it per
repository.

---

## 2. Three layers, one hard boundary

```text
Layer 1   runner        versioned package. All gate logic. Never regenerated per repo.
Layer 2   init          deterministic bootstrap. Probes, writes config, verifies, refuses.
Layer 3   skill         delivery and negotiation. Zero gate logic. Zero policy.
```

**The invariant that makes this work:**

> No policy above Layer 1. No specification knowledge in Layer 3.

Concretely: Layer 3 MUST NOT contain a threshold, a rule list, a file format, a report field,
or an explanation of how a ratchet works. If the skill knows a number, that number is ambient
state on a per-user update cadence, and it can silently disagree with the version installed in
a repository six months ago. Layer 3 calls Layer 2 and reads its output. That is all.

The test for whether a piece of prose belongs in the skill: **could it become wrong when the
runner is upgraded?** If yes, it belongs in the runner or in `.quality/`.

---

## 3. The runner is a standalone tool, not a project dependency

This follows directly from v5.1 §4.4 rather than being a new decision. v5.1 §4.4 requires analyzer
versions to come from `.quality/toolchain.lock` and forbids the BASE worktree from influencing
analyzer selection. Both break if the runner lives in the project's dev dependencies:

```text
runner in project dev-deps
    → runner's own transitive deps resolve against the project environment
    → adding the guardrails changes the project dependency graph
    → deptry's subject (v5.1 §6) is now partly the guardrail system itself
    → a project dependency conflict can make the gate uninstallable
    → the BASE/HEAD environment split (v5.1 §4.4) has a third environment in it

runner as a standalone tool
    → own isolated environment
    → manages analyzer environments keyed on lockfile hash (v5.1 §4.4)
    → project dependency graph untouched; deptry sees only the project
    → guarded repo contains configuration, not code
```

**Requirement.** Installing the guardrails MUST NOT add an entry to the target repository's
`pyproject.toml` dependencies, dev-dependency group, or lockfile. What lands in the repo is
`.quality/`, dedicated dotfiles, a CI workflow, and an AGENTS.md section — configuration and
nothing else. This is the same reasoning as v5.1 §11.2's "no quality configuration in
`pyproject.toml`," extended one step: no quality *code* in the project either.

**Consequence for CI.** The workflow installs the pinned runner as a tool, then runs it. The
runner version is pinned in the workflow and recorded in the report (§10).

---

## 4. Repository layout

One repository, source of truth for all three layers.

```text
/
  README.md
  spec/
    python-ai-guardrails-spec-v5.1.md         frozen, verbatim
    tier3-mutation-testing-addendum-FINAL.md  frozen, verbatim
    aufsicht-distribution-spec-v1.md          this document
  src/aufsicht/
    cli.py                  quality-fast / quality-full / quality-fix / init / upgrade
    base.py                 v5.1 §4.6 base-ref resolution, fails closed
    scope.py                v5.1 §4.2 the one range-intersection filter
    ratchet.py              v5.1 §4.3 per-rule grouping, v5.1 §4.5 exemptions
    toolchain.py            v5.1 §4.4 analyzer env, lockfile-hash cache
    integrity.py            v5.1 §11.2 semantic hashing, protected paths
    allowlist.py            v5.1 §10 expiry, v5.1 §10.1 adapters
    report.py               v5.1 §15 schema, exit codes
    adapters/               one module per tool: ruff, pyright, pytest, pyscn,
                            xenon, semgrep, deptry, pip_audit
    init/                   Layer 2
      probes.py             §6
      writers.py            emits .quality/, CI workflow, AGENTS.md section
      refusals.py           §5.4
  templates/
    quality/                config.toml, ruff.toml, pytest.ini, semgrep rules
    pyrightconfig.json
    workflows/aufsicht.yml
    AGENTS.section.md
  tests/
    fixtures/               one scratch repo generator per self-test case
    test_selftests.py       v5.1 §18 table, executed
    test_tier3_selftests.py addendum §9 table, executed (skipped until Tier 3 built)
  skill/
    SKILL.md                Layer 3
    scripts/bootstrap.sh    byte-identical copy of /install/bootstrap.sh — asserted in CI
  install/
    bootstrap.sh            the single canonical bootstrap
```

**`spec/` holds the frozen documents verbatim.** They are not edited to match the code. If
the code cannot satisfy a frozen document, that is an erratum against the document, made
deliberately and with a changelog entry, in the style both documents already use.

**Semgrep rules ship in `templates/`, not in the skill.** They are policy (v5.1 §9.1, v5.1 §9.2) and
policy lives in `.quality/` in the target repo, protected by v5.1 §11.2.

---

## 5. `aufsicht init` — the Layer 2 contract

This is the installer v5.1 §17 describes, made executable. v5.1 §17's phases are preserved; what follows
adds the contract v5.1 §17 leaves implicit.

### 5.1 Phases

```text
detect   package manager, layout, test runner, task runner, CI provider,
         existing ruff/pyright/pytest config and where it lives
probe    §6 — measure the things that cannot be assumed
propose  print the plan, human-readable and as JSON. Change nothing yet.
write    branch, write files, never to the default branch
verify   run quality-full on the result; run the smoke self-tests
```

`--dry-run` stops after `propose` and is the default when stdout is not a TTY, so an agent
invoking `init` blind gets a plan rather than a mutation.

### 5.2 Exit codes

Distinct from the gate's codes in v5.1 §15, because "the repo is already installed" is not "a gate
failed":

```text
0   installed, or dry-run plan produced
1   refused — see §5.4, the repo needs a human decision first
2   installed with warnings — a probe forced a narrower configuration (§6)
3   tooling error — git unavailable, network failure, unresolvable base ref
```

### 5.3 Idempotency

**Default behaviour on an already-installed repository is detect, report, change nothing,
exit 1.** Overwriting requires `--force`, and `--force` refuses on a dirty tree.

This is not fussiness. A skill is one sentence away at all times, and an `init` that cheerfully
rewrites `.quality/` is a one-sentence reset button for every ratchet, every allowlist entry
and every threshold in the repository — reachable by exactly the actor v5.1 §11 exists to constrain.
Treat re-initialisation as a guardrail change: it goes through v5.1 §11.1 like any other.

### 5.4 Refusals

Each of these exits 1 with the specific remedy printed. None is auto-resolved:

```text
.quality/ already present                    → §5.3
quality config found in pyproject.toml       → must move out per v5.1 §11.2; print what to delete
dirty working tree                           → commit or stash
shallow clone                                → v5.1 §4.6; print the fetch-depth: 0 fix
not a git repository                         → out of scope
no recognised package manager                → out of scope; print what is supported
existing CI workflow named aufsicht.yml      → §5.5
```

### 5.5 What `init` MUST NOT do

* **Merge into an existing CI workflow file.** Editing someone's Actions YAML is not
  deterministic. Write a separate workflow file; collide → refuse.
* **Rewrite AGENTS.md.** Append one delimited section (v5.1 §17). If the delimiters already exist,
  replace only between them.
* **Generate a baseline file.** v5.1 has no baseline at Tier 1 by design (v5.1 §4.3, "not an
  artifact"). Base counts are computed from a worktree at gate time. An installer that writes
  counts to disk has reintroduced Tier 2's staleness problem without Tier 2's machinery.
* **Install unpinned tools.** Exact versions into `toolchain.lock`, never ranges (v5.1 §4.4).
* **Commit to the default branch, or merge anything.** Branch and stop. A human opens and
  merges the PR (v5.1 §17, v5.1 §11.1).
* **Write day-one allowlist entries silently.** v5.1 §17 permits the installer to propose them;
  they appear in the plan with counts before anything is written.

---

## 6. Probes — turning judgment into determinism

Both frozen documents flag assumptions that must be checked against the actual tools rather
than assumed from prose. Some are answered once, globally, and baked into the runner. Others
are properties of the target repository and must be measured at install time. Confusing the
two is how this ends up needing an agent again.

```text
probe                        kind      outcome                        config effect
──────────────────────────   ───────   ────────────────────────────   ──────────────────────
Ruff C901 end_location        global    spans body / def line only     changed-function vs
spans function body?                                                   changed-file scope for
                                                                       C901 (v5.1 §4.2)
Pyright cold start on this    per-repo  under / over the 15s MUST      fast-loop scope narrows
repo                                                                   per v5.1 §5; exit 2
pytest suite wall clock       per-repo  seconds                        written to config as
                                                                       the declared budget (v5.1 §5)
CI provider base-ref env var  per-repo  which variable carries it      v5.1 §4.6 resolution order
mutmut outcome vocabulary     global    state list of pinned version   addendum §4 mapping
os.fork available             per-repo  yes / no                       addendum §3.1 platform
```

**Global probes are answered once and become runner code with a compatibility self-test.**
They are re-run at install as an assertion against the pinned analyzer, not as a decision — if
a global probe disagrees with what the runner believes, that is exit 3 and a version-bump bug,
loudly, not a silent branch.

**Per-repo probes are decisions, and the decision is printed.** "Pyright cold start measured at
22s against a 15s budget, so the fast loop runs Pyright over the changed-file list only" is a
sentence the installer emits, exiting 2. It is not a judgment call an agent makes and forgets
to mention.

This table is where most of the "surely this needs an agent" intuition actually lives, and
almost all of it dissolves on contact: the answers are measurements.

---

## 7. Self-hosting

The repository runs its own guardrails on itself, from the working tree rather than the
published version. A guardrail system that cannot pass its own gate is not evidence of
anything.

**The bootstrap problem, stated rather than discovered.** v5.1 §11.2 protects `.quality/**` and
`.github/workflows/**`. In this repository, `src/aufsicht/**` is also policy in every
meaningful sense — it *is* the evaluator. Protecting it under v5.1 §11.2 would mean every commit to
the project trips the integrity check.

**Resolution.** Two roles, the same shape as v5.1 §17's installer/implementer split:

```text
this repository as a consumer     .quality/** and workflows protected, normally
this repository as the evaluator  src/aufsicht/** is the product, not a protected path.
                                  Its control is code review plus the §8 self-test suite,
                                  which is a stronger control than a path rule anyway.
```

Write this down in the repo's own AGENTS.md. It is exactly the kind of exception that becomes
a security assumption if left implicit.

---

## 8. Self-tests are the definition of done

v5.1 §18 and addendum §9 are already a test suite written as tables. Executing them is what
replaces "the agent believed it implemented the spec."

**Each case is a generated scratch repository**, not a mock: real git history, a real merge
base, a real violation applied, the real CLI invoked. Each asserts three things, and the second
is the one that catches a plausible-looking wrong implementation:

```text
1. the expected gate fails
2. via the expected mechanism   (absolute / diff-scoped / per-rule ratchet)
3. with the expected exit code  (v5.1 §15: 0 / 1 / 3)
```

Plus the negative: `quality-full` on the clean copy passes.

**The cases that matter most are named in v5.1 §18 itself** — the integrity cases and the rename
case, because they test that the system resists what it was built to resist and does not cry
wolf. Add from this document:

```text
init run twice                        → second exits 1, changes nothing
init --force on a dirty tree          → exits 1
init on a shallow clone               → exits 1 with the fetch-depth remedy
init with ruff config in pyproject    → exits 1, prints what to move
init with an existing aufsicht.yml    → exits 1, does not merge
init on a repo with no tests          → installs; pytest gate reports honestly
runner absent from project deps       → assert project lockfile unchanged by install
skill script vs install/bootstrap.sh  → byte-identical, asserted in CI
report carries runner version         → present and matches the installed tool
```

Tier 3 cases from addendum §9 land in the suite from day one, skipped, so the tests exist
before the gate does.

---

## 9. The skill — Layer 3

```text
skill/
  SKILL.md
  scripts/bootstrap.sh
```

`SKILL.md` carries YAML frontmatter with `name` and `description`, then prose. The description
is what triggers it, so it names the situation, not the mechanism.

**The bootstrap is a file the skill runs, not prose the agent reconstructs.** That is what
removes the drift risk — there is nothing to paraphrase.

**Keep the bootstrap thin.** Fifty lines: verify git and the package manager, install the
pinned runner as a tool, hand off to `aufsicht init`. If the bootstrap starts writing
`.quality/` itself, the skill has become a second implementation of Layer 2 on an independent
update cadence, and §2's invariant is gone.

**Byte-identical, enforced.** `skill/scripts/bootstrap.sh` is copied from `install/bootstrap.sh`
by a build step, and CI asserts equality. Hand-syncing two copies is the same drift problem
wearing a different hat.

**What SKILL.md legitimately contains** — the negotiation `init` refuses to guess:

```text
when to invoke, and when not to
how to read init's plan output and explain it in words
the pyproject.toml collision: which of their existing ruff settings survive the move
wiring commands into whatever task runner already exists (make / just / poe / none)
appending to an existing AGENTS.md without trampling it
asking the human for the project test budget
open a branch and a PR; never commit to the default branch (v5.1 §17)
```

**What it MUST NOT contain:** thresholds, rule lists, report fields, ratchet explanations, or
agent behavioural rules. That last one matters — v5.1 §16's rules belong in the target repo's
AGENTS.md, checked in and protected, where they are reviewable. A second copy inside a skill
is neither.

**The pinned runner version appears in three places** — the bootstrap, `toolchain.lock` in the
target repo, and the JSON report. If the skill's copy ever drifts, it shows up as a diff rather
than as two repositories mysteriously behaving differently.

**The skill is not the only path.** CI needs the non-agent entry point, and not everyone uses
an agent harness. `aufsicht init` standalone is the real interface; the skill makes the first
fifteen minutes nicer.

**Worth stating out loud rather than discovering later:** this distributes a system built to
distrust agents, via an agent. That is consistent with v5.1 §17 — the installer has no special
privilege and its output is the first reviewed PR like any other — but it does mean the skill
should be loud about what it changed and cheap to read as a diff, rather than smooth.

---

## 10. Versioning

```text
runner version        semver, tagged, the only version a target repo pins
spec version          the frozen document each release implements (5.1 / addendum FINAL)
config schema_version integer in .quality/config.toml
```

Both the runner version and the spec version it implements appear in the report. Otherwise a
failure six months old is undebuggable in exactly the way v5.1 §4.6 warns about for base SHAs.

```text
MAJOR   report schema change; exit-code meaning change; a new blocking gate;
        a default threshold becoming stricter
MINOR   a new non-blocking signal; a new adapter; a probe added;
        a default threshold becoming looser
PATCH   fixes that do not change what passes
```

**Upgrades never auto-migrate protected files.** `aufsicht upgrade` prints the diff it would
apply to `.quality/` and writes nothing. Applying it is a guardrail-change PR under v5.1 §11.1,
which is the whole point — an upgrade that silently rewrites thresholds is the same hole as
v5.1 §4.4's unpinned analyzer, one level up.

A runner upgrade is a toolchain bump for ratchet purposes (v5.1 §4.5): exempt the affected
analyzers for that PR, and say so in the report.

---

## 11. Non-goals

* No hosted service, no telemetry leaving the repository.
* No plugin system for custom gates in v1. v5.1 §9.1's configurable verification-act list and
  `.quality/` config are the extension surface; arbitrary user code inside the evaluator is a
  bypass surface with extra steps.
* No non-git VCS.
* No Windows support for Tier 3 beyond WSL (addendum §3.1). Tier 1 is unaffected.
* No auto-fix in CI. `quality-fix` exists and is excluded from CI (v5.1 §14).
* No editor integration in v1.
* The runner does not reimplement any analyzer. Every gate shells out to a pinned tool and
  normalises its output. When a gate's logic starts looking like a linter, it is in the wrong
  repository.

---

## 12. Build order

The temptation is to build gate by gate. That gets the spine wrong and then rewrites every
adapter around it.

```text
1  spine        base resolution (v5.1 §4.6), report schema (v5.1 §15), exit codes, config loading
2  tracer       ONE gate end to end — Ruff, diff-scoped — plus its v5.1 §18 self-test
3  ratchet      v5.1 §4.3 grouping, v5.1 §4.4 environments, v5.1 §4.5 exemptions, plus their self-tests
4  gates        remaining adapters against the now-fixed spine
5  integrity    v5.1 §11.2 hashing, protected paths, allowlist and its v5.1 §10.1 adapters
6  init         Layer 2, probes, refusals
7  skill        Layer 3, last, once init's output is stable enough to describe
```

Steps 1–2 are the ones worth being slow about. Every subsequent adapter is cheap if the spine
is right and expensive to retrofit if it is not — the same reason the addendum insists
selection precedes execution.

Do the two global probes from §6 before step 2, not during it. Ruff's `C901` `end_location`
determines whether v5.1 §4.2 gets changed-function or changed-file scope, and both frozen documents
already say to check it before writing the wrapper.

---

## 13. Acceptance criteria

* [ ] one repository holds the frozen specs verbatim, the runner, the templates, the
      self-tests, the bootstrap and the skill
* [ ] runner installs as a standalone tool; target repo's dependency graph and lockfile
      provably unchanged by installation (self-tested)
* [ ] no gate logic outside Layer 1; no threshold, rule list, report field or ratchet
      explanation anywhere in `skill/`
* [ ] `skill/scripts/bootstrap.sh` byte-identical to `install/bootstrap.sh`, asserted in CI
* [ ] bootstrap installs a pinned runner version and hands off to `aufsicht init`
* [ ] runner version and implemented spec version recorded in `toolchain.lock` and the report
* [ ] `aufsicht init` runs standalone without any agent harness
* [ ] `init` phases detect / probe / propose / write / verify, with `--dry-run` defaulting on
      when stdout is not a TTY
* [ ] `init` exit codes 0 / 1 / 2 / 3 per §5.2, distinct from the gate's v5.1 §15 codes
* [ ] `init` on an installed repo exits 1 and changes nothing; `--force` refuses a dirty tree
* [ ] every §5.4 refusal exits 1 and prints a specific remedy
* [ ] `init` never merges into an existing CI workflow, never rewrites AGENTS.md outside its
      delimiters, never writes a baseline, never commits to the default branch
* [ ] global probes re-run at install as assertions; disagreement exits 3, never a silent
      branch
* [ ] per-repo probes emit the decision in words and exit 2 when they narrow configuration
* [ ] `C901` end_location and mutmut's outcome vocabulary answered by running the tools, and
      the answers self-tested against the pinned versions
* [ ] the repository runs its own guardrails, with `src/aufsicht/**` documented as product
      rather than protected path, and the reason written into its AGENTS.md
* [ ] every case in v5.1 §18 executed against a generated scratch repo, asserting gate,
      mechanism and exit code
* [ ] addendum §9 cases present and skipped until Tier 3 is built
* [ ] §8's init-specific cases passing
* [ ] `aufsicht upgrade` prints a diff and writes nothing; a runner upgrade is treated as a
      toolchain bump under v5.1 §4.5 and flagged in the report
* [ ] `SKILL.md` frontmatter present; prose limited to §9's permitted list
* [ ] v5.1 §16's agent rules live in the target repo's AGENTS.md, not duplicated in the skill

---

## 14. Open decisions

Not blockers, but they should be settled deliberately rather than by whoever writes the first
line of code:

**Distribution channel.** A git tag works on day one and needs no name reservation; a package
index gives a shorter install line and real version resolution. The install contract in §5 is
identical either way, so this can be deferred — but reserve the name early if the index is
where this is heading.

**Package and command name — settled: `aufsicht`.** German for oversight or supervision. It is
the only non-English identifier in the system, and it stays that way: every file name, config
key, report field, exit-code name, section title and comment is English. A project name is
read once; a config key is read every time someone debugs a failure, and a bilingual
vocabulary there would be a small tax forever.

```text
github.com/code-factorio/aufsicht      repository
aufsicht                               package, tool, CLI
.quality/                              config directory — unchanged, spec-mandated
.github/workflows/aufsicht.yml         emitted workflow
```

Unregistered on PyPI as of writing, in both plain and `-cli` forms. Register it before it
reaches the workflow template and every target repository's AGENTS.md, even if distribution
starts from a git tag — an index 404 is not a reservation.

**Subcommands vs shims — settled: short English subcommands.** `aufsicht quality-fast` stutters,
so the subcommands drop the prefix and v5.1 §14's names live in the task runner, which is where
that section puts them anyway:

```text
aufsicht fast      → task runner exposes it as quality-fast
aufsicht full      → quality-full
aufsicht fix       → quality-fix
aufsicht init
aufsicht upgrade
```

One tool to pin and version, and the names v5.1 specifies are still what anyone types.

**Whether the skill ships inside this repository or separately.** Inside keeps the
byte-identical assertion trivial and is the assumption in §4. Separately decouples the release
cadences, at the cost of the thing §9 exists to prevent.
