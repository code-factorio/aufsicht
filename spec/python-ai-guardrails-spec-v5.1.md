# Specification: Python AI Code Quality Guardrails (v5.1 — frozen for Tier 1)

> **Status: frozen.** v5.1 applies errata only; no architecture changed. Tier 1 is
> specified to implementation detail. Further abstract design work has lower expected
> value than observing which gates real agents trip, evade, or false-positive against.
> Build it, run it for two weeks, then revisit.

## 0. Changelog

**v5 → v5.1 — errata, no new mechanisms:**

1. **Analyzer environment and project dependency environment separated** (§4.4). v5's "never
   resolve dependencies in the BASE worktree" was too broad: Pyright and deptry read the
   project environment, so analysing BASE source against HEAD's dependencies manufactures
   diagnostics that never existed at BASE. Adds the case v5 missed — when the project
   lockfile itself changes, no configuration makes the comparison sound, so the affected
   ratchets are exempted like a toolchain bump.
2. **Ratchet exemption is tool-level by default** (§4.5). Rule-set difference was too narrow;
   `target-version`, `preview`, `per-file-ignores`, `exclude` and rule settings all move
   counts without changing the enabled rule set. Rule-level narrowing is now an explicit
   optimisation, not the baseline behaviour.
3. **Circular imports reconciled** (§3, §19). The Tier 1 table and acceptance criteria still
   said `pyscn check --max-cycles 0`, which §10.1 had already rejected in favour of
   structured analysis minus allowlist.
4. **Stale 3-minute MUST removed** from §14 and §19, which §5 had already superseded.
5. Resolved base SHA recorded in the report (§4.6, §15). Which config each run uses is now
   stated explicitly rather than implied (§4.3).

**v4 → v5** — implementation-level corrections, no new tools or layers:

1. **The toolchain is pinned and protected** (§4.4). A ratchet is only valid if BASE and
   HEAD are measured by the same instrument. A Ruff upgrade that adds diagnostics produces
   a regression that isn't one.
2. **Ratchet exemptions are scoped to newly enabled rules**, not to whole tools or whole
   PRs (§4.5).
3. **Base-ref resolution is a specified contract that fails closed** (§4.6), including the
   shallow-clone case that silently disables ratchets under default CI settings.
4. **"Diff-scoped" is defined per gate** (§4.2) via one range-intersection filter rather
   than three ad-hoc implementations.
5. **Allowlist adapters specified for absolute tools** (§10.1). Circular dependencies are
   allowlistable via structured analysis, so one legacy cycle cannot block adoption.
6. **Tier 2 rename contradiction resolved** (§18, §19) in favour of accepting the false
   positive. No secondary rename detector.
7. **Time budget split** (§5). `quality-fast < 15s` stays a MUST; whole-repo wall clock
   cannot be legislated for arbitrary repositories.
8. Installer vs implementation agent roles disambiguated (§17).

**v3 → v4:**

1. **Ratchets are per rule, not per total** (§4.3). A total-count ratchet passes when you
   fix one old error and introduce one different new one. Grouping by rule id costs
   nothing and closes most of that gap. Includes handling for null rule ids and for PRs
   that legitimately enable a new rule.
2. **Guardrail approval is organised by deployment model** (§11). v3 claimed an agent
   "cannot produce a commit signature," which is false whenever the agent runs in the
   developer's own shell with access to `SSH_AUTH_SOCK`, a warm gpg-agent, or the
   keychain. Signing strength depends on credential isolation, not on the signature.
3. **`added_by` demoted to provenance metadata** (§10). It is not a security property —
   an agent can write `added_by = "human"`. Enforcement is the protected-path rule alone.
4. **Section hashing must be semantic, not byte-level** (§11.2). Parse, extract, sort,
   canonicalize, then hash — otherwise reformatting trips integrity protection.

Unchanged from v3 and worth restating because it is easy to misread: repo-wide Pyright is
**not** replaced by changed-file Pyright. The diff-scoped run is for `quality-fast`; the
repo-wide per-rule ratchet in `quality-full` is what catches a signature change in one file
breaking an unchanged consumer in another (§4.2, §12).

**v2 → v3**, all five from review plus one self-caught bug:

1. **Ruff `S` severity gates removed.** Ruff carries no severity metadata; the gate was
   unimplementable. Replaced with an enumerated rule list at zero findings (§6, §8).
2. **pip-audit no longer filters by severity.** A large share of advisories carry no CVSS
   score, so severity filtering silently drops them. All findings block or are allowlisted (§8).
3. **"New" removed from every Tier 1 gate.** Each gate now names its enforcement
   mechanism — absolute, diff-scoped, or per-rule ratchet — none of which need fingerprinting (§4).
4. **Guardrail approval is credential isolation, not a label.** The label mechanism was
   wrong: an agent with a forge token can apply labels. Restated as a property with
   commit signing as the strong form (§11).
5. **Tier 3 contradiction resolved and test verification broadened.** Human sign-off is
   required at every tier; fresh-context agent review is optional. Assertion detection
   reframed around observable verification (§9, §13).
6. **`pyproject.toml` section-level protection was vacuous** — `git diff --name-only`
   cannot see TOML sections. All quality config moves out of `pyproject.toml` (§11, §17).

---

## 1. Purpose

Add a deterministic, reusable quality gate to an **existing** Python repository so AI
coding agents cannot silently degrade it.

> Quality checks MUST be deterministic wherever practical. AI instructions supplement
> deterministic validation; they never replace it.

> The guardrail system MUST be small enough that a human can read all of it. A guardrail
> system nobody understands is technical debt with a compliance badge.

| Kind of finding | Mechanism | Blocks? |
|---|---|---|
| Hard correctness / policy violation | absolute, diff-scoped, or ratcheted rule | always |
| Structural regression | baseline comparison (Tier 2) | yes, overridable via allowlist |
| Quality metric | telemetry | never |
| Subjective design quality | human review, optionally agent-assisted | advisory to a human |

---

## 2. Non-goals

The system MUST NOT:

* require restructuring the repository into a prescribed layout
* prohibit idiomatic Python without a concrete correctness reason — specifically **not**
  default arguments, module-level constants, `dict.get()`, `getattr(..., default)`,
  `or`-fallbacks, or `Optional`
* force an agent to repair unrelated pre-existing debt
* rely on an LLM to decide whether quality is acceptable
* compress metrics into a single score used as a gate
* mutate source code during validation
* exceed the time budgets in §5

---

## 3. Tiered delivery

**Build Tier 1. Run it two weeks. Then decide whether Tier 2 earns its maintenance cost.**

Tier 2's regression engine is real software. Most of its value comes from checks Tier 1
already covers without a baseline.

### Tier 1 — no baseline artifact required

Every gate uses one of three mechanisms, defined in §4. None requires fingerprinting.

| Check | Tool | Mechanism |
|---|---|---|
| Lint + format | Ruff | diff-scoped + per-rule ratchet |
| Types | Pyright (`basic`) | diff-scoped + per-rule ratchet |
| Tests | pytest | absolute (green) |
| Suppression comments | added-line scan + Ruff `PGH003/PGH004/RUF100` | diff-scoped |
| Test disabling | Semgrep | diff-scoped |
| Test verification | Semgrep | diff-scoped |
| Per-function complexity | Ruff `C901` ≤ 10 | diff-scoped |
| Aggregate complexity | Xenon | count ratchet (single metric) |
| Circular imports | pyscn structured dep analysis, §10.1 | absolute (zero unallowlisted) |
| Dead code | pyscn | per-rule ratchet |
| Security patterns | enumerated Ruff `S` rules | diff-scoped + per-rule ratchet |
| Vulnerabilities | pip-audit | absolute (all findings) |
| Dependency hygiene | deptry | per-rule ratchet (DEP001–DEP005) |
| Guardrail integrity | §11 | absolute |

### Tier 2 — fingerprinted regression analysis

Adds `quality-baseline` / `quality-diff`, the §7 fingerprint scheme, and per-finding
duplication / dead-code / coupling deltas. Upgrades Tier 1's ratchets to identity-matched
comparison so "added one, fixed one" no longer passes.

### Tier 3 — optional

Mutation testing on changed files, architecture tests, `diff-cover`, coupling budgets,
fresh-context agent design review.

---

## 4. Enforcement mechanisms

v2 used the word "new" in gates that could not compute newness without the fingerprinting
it had deferred. Every gate now declares which of these it uses.

### 4.1 Absolute

Whole repo, threshold zero, no comparison. Use when the finding is unambiguous and cheap
to fix, so a legacy exemption would just mean it never gets fixed.

Applies to: circular imports, test failures, guardrail integrity, pip-audit findings.

### 4.2 Diff-scoped

v4 used "diff-scoped" for three different things — changed file, added line, changed
function — which are not interchangeable. Running Semgrep over changed files reports an
untouched legacy `@pytest.mark.skip` elsewhere in the file; running Ruff over changed files
reports a legacy `C901` in a function nobody edited.

Every gate declares its scope explicitly:

```text
gate                      scope
────────────────────────  ─────────────────
Ruff lint (general)       changed files
Ruff S rules              changed files
Pyright (fast loop)       changed files
suppression comments      added lines
skip / xfail              added lines
trivial assertions        added lines
verification absence      changed test functions
C901                      changed functions (see below)
```

**One filter, not three.** Every tool emits findings with a location and most with an
end location. Implement a single helper and configure the predicate per gate:

```python
def in_scope(finding, changed_files, added_lines, mode):
    if mode == "file":     return finding.path in changed_files
    if mode == "line":     return bool(range(finding.start, finding.end+1) & added_lines)
    if mode == "function": return bool(finding.span & added_lines)  # requires a span
```

`added_lines` comes from `git diff -U0` hunk headers, per file.

**Changed-function scope for C901.** Ruff's JSON output carries `location` and
`end_location` per diagnostic. If the `C901` range spans the function body, hunk overlap
gives changed-function semantics with no AST layer. **Verify this during implementation.**
If the range covers only the `def` line, do **not** build a diff→AST mapper for Tier 1 —
change the policy to *changed files must satisfy C901* and accept the stricter treatment of
legacy functions. Do not claim changed-function semantics while implementing changed-file
semantics.

**Known limit:** a change in file A can create an error in unchanged file B. Diff scoping
misses it. The per-rule ratchet catches it.

### 4.3 Per-rule count ratchet

Run the tool at the merge base and at HEAD, group findings by **rule id**, and compare each
bucket independently:

```text
for each rule r in (BASE.keys ∪ HEAD.keys):
    HEAD[r] <= BASE[r]   → pass
    HEAD[r] >  BASE[r]   → fail, reporting r and both integers
```

A total-count ratchet is much weaker than this for no saving: fixing one `F401` while
introducing one `B006` nets to zero and passes. Grouping by rule costs one `groupby` and
closes that case.

Both tools already emit what is needed. `pyright --outputjson` carries `rule` per
diagnostic; `ruff check --output-format json` carries `code`; `deptry` findings carry
`DEP001`–`DEP005`; pyscn's JSON carries a finding type.

**Null rule ids.** Pyright emits some diagnostics with `rule: null` — syntax errors, certain
unresolved imports, configuration errors. These MUST be bucketed under a reserved key
`"<no-rule>"` and ratcheted like any other, or they disappear from the comparison entirely
and become a free channel for regressions.

**Which configuration each run uses.** BASE source is analysed under **BASE's** tool
configuration; HEAD source under **HEAD's**. Running BASE source under HEAD's config
evaluates a policy against source that was never written for it, and produces numbers that
mean nothing. Where the two configurations genuinely differ, §4.5's exemption handles the
resulting incomparability — that is what it is for.

Consequently, a rule removed from configuration between BASE and HEAD appears in the BASE
map with some count and is **absent from HEAD**, so `HEAD[rule] = 0 <= BASE[rule]` and the
ratchet is satisfied naturally. No special handling needed.

**Newly enabled rules and other config changes.** See §4.5.

**Where this stops.** Per-rule-per-file would be tighter still, but it is unstable under
file moves and renames, which is the identity problem Tier 2 exists to solve properly.
Per-rule is the last useful step before you are building fingerprints badly.

**Residual gameability.** Two instances of the same rule in different files still swap out
cleanly. Accepted for Tier 1.

**Not an artifact.** The base counts are computed from a `git worktree` at the merge base at
gate time. They MUST NOT be committed to the repository. A committed count file is a
baseline, with all the staleness problems Tier 2's `quality-baseline` handles explicitly and
this does not.

Cache the merge-base run keyed on the base commit SHA — otherwise every ratcheted gate runs
the tool twice and §5's budgets are unreachable.

### 4.4 Toolchain pinning

> **A ratchet comparison MUST NOT compare diagnostics produced by different versions of the
> same analyzer.**

A ratchet assumes BASE and HEAD were measured by the same instrument. Nothing guarantees
that by default. Ruff ships new rules and changes existing rule behaviour frequently;
Pyright's inference changes between releases; pyscn is young and moving fast. Upgrade any of
them and `base[rule] = 4`, `head[rule] = 7` with the application code untouched.

**`.quality/toolchain.lock`** pins exact versions of every quality tool. It is a protected
path (§11.2). `quality-fast` and `quality-full` MUST execute tools from that environment and
no other.

**The two environments.** v5 said "never resolve dependencies inside the BASE worktree." That
is too broad. Pyright resolves imports against the installed project environment and deptry's
entire subject is the dependency graph, so analysing BASE source against HEAD's dependencies
manufactures `reportMissingImports` and DEP00x findings that never existed at BASE. Two
distinct things must be held constant or varied deliberately:

```text
Analyzer environment       SAME for both runs.
                           Exact versions from HEAD's .quality/toolchain.lock.
                           The BASE worktree MUST NOT influence analyzer selection.

Project dependencies       Per commit. BASE source resolves from BASE's project lockfile,
                           HEAD source from HEAD's, where the analyzer needs them at all.
```

The invariant is not "never install in the BASE worktree." It is:

> **The BASE worktree MUST NOT choose the analyzer versions.**

```text
tool environment ← HEAD .quality/toolchain.lock          (one, shared)
    ├─ run against HEAD source + HEAD project deps       → head counts
    └─ run against BASE source + BASE project deps       → base counts
```

**When the project lockfile itself changes, the comparison is unsound regardless.** If HEAD
removes a library, HEAD legitimately has different diagnostics; if HEAD adds one, BASE cannot
resolve it. No arrangement of environments fixes this, because the environments genuinely
differ. Treat it exactly like a toolchain bump (§4.5): when the project lockfile differs
between BASE and HEAD, **exempt the environment-sensitive ratchets — Pyright and deptry — for
that PR**, and record `dependency_environment_changed: true` in the report. Ruff and pyscn are
unaffected; they do not read the installed environment.

**Cost.** Two dependency resolutions is slow. Cache environments keyed on the lockfile hash,
and when the lock is unchanged — the common case, and the only case where these ratchets are
sound anyway — reuse a single environment for both runs.

Exact versions, not ranges. `ruff>=0.14` combined with `uv`'s `exclude-newer` still resolves
differently on different days, so a range gives you a drifting instrument that looks pinned.

A toolchain upgrade is a guardrail-change PR (§11) and establishes a new reference on merge,
with exemptions per §4.5.

### 4.5 Ratchet exemptions

v4 skipped every ratchet whenever guardrail configuration changed. That is far wider than
needed: enabling a Ruff rule should not disable the Pyright, deptry and pyscn ratchets.

**Default: exempt the affected analyzer, for that PR only.**

```text
approved config change affecting Ruff     → Ruff ratchets exempt
approved config change affecting Pyright  → Pyright ratchets exempt
toolchain.lock bumps a tool               → that tool's ratchets exempt
project lockfile changed (§4.4)           → Pyright and deptry ratchets exempt
deptry / pyscn config unchanged           → those ratchets run normally
```

v5 scoped the exemption to the **enabled-rule set difference**, which was too narrow. A rule
code can stay identical while its results move, via:

```text
target-version          preview mode           per-file-ignores
exclude paths           mccabe max-complexity  other rule settings
Pyright typeCheckingMode                       Pyright execution environments
```

None of these changes the enabled rule set, and all of them change counts. Tool-level
exemption is the safe Tier 1 behaviour and still preserves every unrelated ratchet.

**Optional narrowing.** The elegant case is worth keeping if the tool-level exemptions become
annoying in practice: if the parsed config diff for a tool is confined to **additions to
`select` / `extend-select`**, exempt only the newly added buckets rather than the whole tool.
The machinery already exists — §11.2's semantic config hashing gives you a parsed structural
diff, so "is this change confined to the select keys" is answerable without new code. Treat it
as an optimisation to add once you have seen how often config changes actually happen, not as
Tier 1 scope.

**On "no application changes in guardrail PRs":** not mechanically enforceable, because
enabling a rule frequently requires fixing its violations in the same PR. The protection you
actually want is already present — non-exempted tools ratchet normally, so a feature smuggled
into a guardrail PR is gated like any other. The gate therefore **reports** the count of
non-protected files touched in a guardrail PR as a reviewer signal, and does not fail on it.

### 4.6 Base reference resolution

`git merge-base HEAD origin/main` is too specific for "any Python repo." Repositories use
`master`, non-`origin` remotes, stacked branches, merge queues and shallow checkouts.

Resolution order:

```text
1. CI-provided base SHA         (GITHUB_BASE_REF / equivalent; also covers merge queues,
                                 where HEAD is a synthetic merge commit)
2. QUALITY_BASE_REF             from .quality/config.toml
3. remote HEAD                  git symbolic-ref refs/remotes/<remote>/HEAD
4. TOOLING ERROR, exit 3
```

The contract is that CI supplies **either an immutable base commit SHA, or a base ref the
guardrail resolves to one**. Which environment variable carries it differs per provider and
MUST be verified against each during implementation rather than assumed from this document.

**Record the resolution.** The report includes the resolved identity, and all ratchet work
operates on that SHA rather than on a ref that could move mid-run:

```json
{"base": {"source": "ci", "ref": "refs/heads/main", "sha": "abc123..."}}
```

`source` is one of `ci`, `config`, `remote-head`. Without this recorded, a ratchet failure is
undebuggable — you cannot tell whether the numbers came from the commit you assumed.

**Fail closed.** If the merge base cannot be resolved, exit 3 and report why. Never fall
back to analysing HEAD alone — that silently disables every ratchet while the gate still
reports green, which is the worst available failure mode.

**Shallow clones.** `actions/checkout` defaults to `fetch-depth: 1`, and on a shallow clone
`git merge-base` either fails or returns a wrong answer. This is the default configuration,
not an edge case. The gate MUST check:

```bash
[ "$(git rev-parse --is-shallow-repository)" = "true" ] && exit 3
```

and the installer MUST emit CI with `fetch-depth: 0`.

---

## 5. Time budgets

Non-negotiable. An agent that finds the gate slow batches its edits and stops using it as
a feedback loop, which is the entire mechanism.

| Gate | Budget | Strength | Scope |
|---|---|---|---|
| `quality-fast` | **< 15 s** | MUST | diff-scoped only, no ratchets |
| `quality-full`, static analysis only | < 3 min | SHOULD | whole repo, ratchets included |
| `quality-full`, project test suite | repo-specific | declared in config | |
| `pip-audit` (network) | CI budget | not inner-loop | |
| `quality-diff` | < 90 s | SHOULD | Tier 2 |

v4 made `quality-full < 3 min` a universal MUST. That cannot hold for arbitrary
repositories — a legitimate pytest suite can run for twenty minutes, and pip-audit's
dependency resolution is network-bound. The guardrail spec does not get to legislate someone
else's test suite. The static-analysis overhead the guardrails themselves add is the part
this spec is responsible for, and that stays under three minutes.

`quality-fast < 15 s` remains a hard MUST, because it is the agent feedback loop and the
entire mechanism depends on it being cheap enough to run after every edit.

`quality-fast` deliberately excludes ratchets — they need a second tool run and blow
the budget. Ratchets belong to `quality-full` and CI.

If `quality-fast` exceeds budget, narrow scope rather than raise the budget: Pyright over
the changed-file list, pytest limited to tests touching changed modules, Semgrep restricted
to the custom ruleset.

Pre-commit runs `quality-fast` only. CI is the real gate. A hook slow enough to provoke
`--no-verify` is worse than no hook, because it trains the habit.

---

## 6. Toolchain

| Tool | Owns | Notes |
|---|---|---|
| Ruff | format, lint, `C901`, enumerated `S` rules, `PGH`, `PT`, `ERA` | |
| Pyright | types | `basic` repo-wide, `strict` on an opt-in directory list |
| pytest + coverage | behaviour, branch coverage | |
| pyscn | clones, dead code, CBO, cycles | primary structural source |
| Xenon | aggregate complexity only | `--max-average`, `--max-modules` |
| Semgrep | anti-evasion + repo-specific invariants | small custom ruleset |
| deptry | dependency hygiene | |
| pip-audit | vulnerabilities | |
| Bandit | **optional**, full gate only | see §6.1 |
| pytestarch | architecture invariants | Tier 3, only if real layers exist |

### 6.1 Security rules — the Ruff `S` correction

v2 gated on "new Ruff S-rule HIGH-severity finding." **Ruff emits no severity or confidence
metadata.** It is a rule port, not a bandit wrapper, so that gate could never have been
implemented. Enumerate instead:

```toml
# .quality/ruff.toml
[lint]
extend-select = [
  "S102",  # exec
  "S105", "S106", "S107",  # hardcoded password / secret
  "S301",  # pickle load
  "S302",  # marshal
  "S306",  # mktemp
  "S307",  # eval
  "S311",  # non-cryptographic RNG in a security context
  "S324",  # weak hash (md5, sha1)
  "S501",  # requests with verify=False
  "S506",  # unsafe yaml.load
  "S602", "S605",  # shell=True / os.system
  "S608",  # SQL built by string concatenation
]

[lint.per-file-ignores]
"tests/**" = ["S101", "S105", "S106", "S311"]
```

Gate: **zero findings, diff-scoped.** No severity filter, because there is nothing to
filter on.

`S101` (bare `assert`) is deliberately absent from the list — pytest is built on `assert`,
and enabling it repo-wide creates noise that trains people to ignore the `S` prefix.

**What is lost:** bandit's severity/confidence triage, and roughly the rules Ruff hasn't
ported. Teams that want coarse triage on a large legacy surface MAY keep Bandit in
`quality-full` under a per-rule ratchet. Do not run it in `quality-fast` — it is slow enough
to blow the budget on its own.

### 6.2 Complexity ownership

Three tools measure complexity. Exactly one gates each scope:

* Ruff `C901`, threshold 10 — per-function. Diff-scoped hard gate.
* Xenon `--max-average A --max-modules B --max-absolute C` — aggregate. Count ratchet.
  `--max-absolute` is deliberately loose so it never fights `C901`.
* pyscn complexity output — telemetry only, never gated.

Aggregate is the one that matters most here. Per-function gates catch the single monster;
AI code degrades as a drift where nothing individually trips a threshold and every module
is a swamp.

### 6.3 Removed

Radon as a separate step — it ships as Xenon's engine; invoke `radon cc --json` ad hoc for
telemetry. The Maintainability Index entirely — a Halstead-derived formula that mostly
measures "is this file long and comment-free," and gating on it teaches agents to pad
comments.

---

## 7. Finding identity (Tier 2 only)

Baseline comparison is only as good as the ability to say "this is the same finding."
`file:line` is not stable — one inserted line shifts everything below and produces phantom
regressions, which is how agents learn to treat a gate as noise.

```text
fingerprint = sha1(rule_id | normalized_path | enclosing_symbol_path | context_hash)
```

* `normalized_path` — repo-relative, POSIX separators
* `enclosing_symbol_path` — dotted path to the innermost containing `def`/`class`,
  e.g. `services.billing.Invoice.render`; module level uses `<module>`
* `context_hash` — sha1 of the enclosing symbol's source with whitespace collapsed,
  comments stripped, string and number literals replaced by type placeholders; truncated
  to 8 hex chars
* Line numbers are recorded for display, never part of the fingerprint

Accepted consequences, stated so nobody is surprised in month three:

* **Renaming a symbol invalidates its fingerprint.** The old finding reads as resolved, the
  new one as introduced, and `quality-diff` **fails**, requiring human review to clear. A
  rename plus a real regression in the same symbol is still caught; a pure rename costs one
  review. A secondary content-signature matcher could distinguish rename from copy, but do
  not build it until real usage proves the false positives painful — the alternative,
  content-only hashing, cannot distinguish a moved function from a copied one, which is a
  worse trade for this purpose.
* **Duplication findings are pairwise** and unstable when either endpoint changes.
  Duplication is therefore compared in aggregate only (§8), never per finding.
* **Moving a symbol between files invalidates its fingerprint.** Same reasoning as rename.

`quality-diff` MUST report `introduced`, `resolved` and `unchanged` as separate sets. A
large `resolved` set alongside a large `introduced` set almost always means code was moved,
not fixed, and the report must say so.

---

## 8. Thresholds

All configurable in `.quality/config.toml`. These defaults exist so the spec is
implementable without further negotiation.

### Hard gates — any occurrence fails

```text
mechanism      gate                                              threshold
─────────────  ────────────────────────────────────────────────  ─────────
absolute       circular dependency                               0
absolute       test failure                                      0
absolute       pip-audit finding (any severity, incl. unscored)  0
absolute       protected-path modification without approval      0
absolute       expired allowlist entry                           0
diff-scoped    suppression comment on an added line              0
diff-scoped    undocumented skip / xfail on an added line        0
diff-scoped    test with no observable verification              0
diff-scoped    Ruff error in a changed file                      0
diff-scoped    enumerated Ruff S-rule finding in a changed file  0
diff-scoped    Pyright error in a changed file                   0
diff-scoped    C901 complexity of a changed function             > 10
per-rule       Pyright diagnostics, grouped by rule              > base[rule]
per-rule       Ruff findings, grouped by code                    > base[code]
per-rule       pyscn dead-code findings, grouped by type         > base[type]
per-rule       deptry findings, grouped by DEP00x                > base[code]
ratchet        modules over Xenon --max-modules                  > base
```

The per-rule rows compare bucket by bucket (§4.3), so fixing an `F401` does not buy you a
`B006`. Xenon stays a single-integer ratchet because it produces one metric, not a
classified finding set.

**pip-audit at zero, all severities.** v2 gated HIGH and CRITICAL only. A large share of
PyPI/OSV advisories carry no CVSS score at all, so a severity filter silently drops them,
and severity is contextual anyway — a MEDIUM in your request path outranks a HIGH in a
dev-only package you never invoke. Gate everything; §10 handles the rest.

On a legacy repo this produces perhaps twenty to fifty allowlist entries on day one. That
is the intended workflow, not a failure: each entry carries a reason and an expiry, so the
backlog is visible and dated instead of invisible and permanent.

### Regression gates — Tier 2, overridable via reviewed allowlist

```text
project average cyclomatic complexity     increase > 0.3 absolute
duplicated-line ratio (pyscn)             increase > 1.0 percentage point
max CBO of any changed module             increase > 3
diff coverage on changed lines            < 70%
overall coverage                          decrease > 0.5 percentage point
```

v2's "SLOC growth without test growth" gate is **removed** — it is satisfied by adding one
trivial test file, so it measured nothing.

### Telemetry — recorded, never gated

```text
SLOC, LLOC, module count, complexity distribution,
pyscn health score, per-module CBO, Halstead metrics
```

**On gameability:** counting gates are gameable. "No changed function over C901 10" is
satisfiable by splitting one complex function into two mediocre ones coordinating through
a shared mutable dict — worse code that passes. This is why aggregate complexity is
ratcheted and why §13 exists.

---

## 9. Test quality

v2 covered agents *disabling* tests but not agents writing tests that pass while verifying
nothing, which is more common and more damaging. A test that mocks the unit under test and
asserts on the mock is worse than no test: it occupies a coverage slot and communicates
false safety.

### 9.1 Observable verification, not bare asserts

v2's rule — zero `assert` and zero `pytest.raises` → FAIL — was too narrow and would have
failed a pile of legitimate tests. The gate is on the absence of any **observable
verification act**, where the set is configurable and ships with:

```toml
# .quality/config.toml
[test_verification]
statements = ["assert"]
calls = [
  "pytest.raises", "pytest.warns", "pytest.deprecated_call",
  "self.assert*", "self.fail",                 # unittest
  "assert_*", "check_*", "verify_*",           # helper naming convention
  "*.assert_called*", "*.assert_has_calls",    # mock, weak but observable
  "snapshot", "verify",                        # syrupy / approvaltests
]
```

A test function fails only when its body contains **none** of these. Teams add their own
assertion helpers to `calls` rather than suppressing the rule.

### 9.2 Trivially-passing verification

Separately gated, diff-scoped, zero tolerance:

```text
assert True / assert 1 / assert not False
assert x == x
assert isinstance(x, object)
assert <name>            where <name> is bound to a Mock or MagicMock in the same body
```

That last one is worth its own rule. `MagicMock` is unconditionally truthy, so
`assert result` on a mock can never fail, and it is a very common AI output when a test is
written to pass rather than to check.

### 9.3 Residual

A test can contain a real assert and still verify nothing meaningful — `assert result is
not None` is the canonical case. No static rule reaches this. Tier 3 addition: `mutmut`
scoped to changed files, CI only, gate at **≥ 60% mutants killed on changed lines**. It is
the only mechanism that mechanically distinguishes a real test from a coverage-shaped one.

Assume a human reads the tests in the diff. §13.

---

## 10. The escape valve

Exactly one sanctioned mechanism. Without a sanctioned exit, agents invent an unsanctioned
one, and that one is not reviewable.

`.quality/allowlist.toml`:

```toml
[[entry]]
fingerprint = "a3f9c21e"          # Tier 2; Tier 1 entries use rule + path
rule        = "pip-audit/GHSA-xxxx-yyyy-zzzz"
path        = "pyproject.toml"
reason      = "No fixed release upstream; package is dev-only and not imported at runtime. Tracked in ISSUE-412."
added_by    = "human"
added_on    = "2026-08-16"
expires     = "2026-11-16"
```

* CI **fails** on any entry past `expires`. No silent permanence.
* Maximum lifetime 180 days.
* Empty `reason`, or under 20 characters, fails validation.

**`added_by` is provenance, not enforcement.** An agent can write `added_by = "human"` as
easily as a human can. The field exists so a reviewer reading the file six months later
knows where an entry came from. It carries no security weight and must never be described
as though it does.

The actual control is that `.quality/allowlist.toml` is a protected path (§11): any change
to it trips the integrity check and requires approval from an independent principal.
An agent cannot add an entry because it cannot merge a change to this file, not because of
anything it writes inside the file.

CI MAY additionally fail on `added_by = "agent"` as a cheap tripwire for an honest agent
that self-reports. Label it as such in the implementation. It catches mistakes, not
adversarial behaviour, and treating it as a control is exactly the confusion this
paragraph exists to prevent.

No inline suppression comments. No per-tool ignore files. One file, greppable, reviewable
in a diff, every exception dated.

### 10.1 Adapters for absolute tools

An absolute gate and a central allowlist are in tension: `pyscn check --max-cycles 0` cannot
mean "zero except these three approved legacy cycles." Each absolute tool needs a stated
adapter.

**pip-audit.** Native support for repeated `--ignore-vuln ID`. The adapter reads allowlist
entries with `rule = "pip-audit/*"` and emits one flag each. Store every known alias for a
vulnerability (GHSA, PYSEC, CVE) in the entry — pip-audit may report a different alias after
an advisory database update, and an ignore keyed on the wrong one silently stops matching.

**Circular dependencies.** Two options; **B is chosen.**

* *Option A* — cycles are never allowlistable; Tier 1 cannot activate until the repository
  has zero. Rejected: one ugly legacy cycle blocks adoption of the entire package, which is a
  bad trade for a system whose premise is working with existing repositories.
* *Option B* — run structured analysis, subtract approved cycles, fail on the remainder:

  ```text
  pyscn analyze --json --select deps
    ↓ extract cycles
    ↓ canonicalize each cycle
    ↓ remove those matching an allowlist entry
    ↓ remaining count > 0 → FAIL
  ```

  **Canonicalisation is required.** The same cycle can be reported starting from any member,
  so `a→b→c→a` and `b→c→a→b` are the same cycle with different serialisations. Rotate to
  start at the lexicographically smallest module, keep direction, then hash. Without this,
  allowlist entries stop matching between runs and the exception silently expires.

  A cycle allowlist entry MUST carry a shorter expiry than the 180-day default — 90 days —
  because cycles are cheap to fix and the entry exists to unblock adoption, not to become
  permanent.

**Everything else** (test failures, protected-path modification, expired entries) is
genuinely absolute and has no adapter. There is no legitimate reason to allowlist a failing
test.

---

## 11. Guardrail integrity

v2 asked the agent to explain itself when modifying the evaluator, and proposed a PR label
as the human override. **Both were wrong.** An agent willing to weaken a check will write a
convincing explanation, and an agent holding a forge token that can open a PR can also
apply a label to it.

### 11.1 The property

> Approval to change guardrail configuration MUST be authenticated by credentials
> unavailable to the implementing agent.

v3 named commit signing as the strongest mechanism on the grounds that "the agent cannot
produce the signature." **That is false in the most common deployment.** An agent running in
the developer's shell inherits `SSH_AUTH_SOCK`, a warm gpg-agent, and the keychain, so a
signed commit proves only that *a process running with the developer's credentials* created
it. Git supports GPG, SSH and S/MIME signing; none of them establishes an independent
principal on its own. Isolation does that, and the signature merely records it.

So the mechanism follows from where the agent runs, not from a ranking:

**A — Agent runs in CI or a sandbox under its own identity.** It holds a scoped token and
no human signing key.
→ Require a signed commit on protected paths, verified in CI against an allowed-signers
file. Combine with CODEOWNERS. This is a genuine boundary.

**B — Agent runs in the developer's shell as the developer.** It inherits every local
credential, so **no local mechanism is a control.** Signing, pre-commit hooks, file
permissions and environment markers are all reachable by a process running as you.
→ The boundary must be server-side and must involve a second person: branch protection with
required review from a CODEOWNER who is not the PR author. The agent can commit anything
locally; it cannot merge it.

**C — Solo developer, local agent, no second reviewer.** Nobody else can approve, and
self-approval is available by definition.
→ **There is no enforcement here, and the spec should not pretend otherwise.** The integrity
check degrades to a tripwire: it fails the local gate, prints exactly which protected paths
changed, and forces the change to appear as a separate reviewed commit rather than buried in
a feature diff. That is a real benefit and it is not a security control. Write it down as a
tripwire so nobody later builds a threat model on top of it.

A PR label is **not** an acceptable mechanism in any model. Any agent holding a token that
can open a pull request can also label one.

### 11.2 Protected paths

```text
.quality/**                  all config, toolchain.lock, allowlist, baseline, semgrep rules
pyrightconfig.json
.pyscn.toml
.pre-commit-config.yaml
.github/workflows/**         (or equivalent CI definitions)
AGENTS.md
```

`.quality/toolchain.lock` is protected for a reason distinct from the others: an agent that
can bump Ruff can shift every ratchet reference without touching a threshold (§4.4).

**`pyproject.toml` is deliberately absent.** v2 listed it as protected "for the
`[tool.ruff*]` sections," but `git diff --name-only` operates on paths and cannot see TOML
sections. Protecting the whole file breaks every dependency bump; protecting a section is
not expressible. Therefore:

* All quality configuration lives outside `pyproject.toml`: `.quality/ruff.toml`,
  `.quality/toolchain.lock`,
  `pyrightconfig.json`, `.pyscn.toml`, `.quality/config.toml`, `.quality/pytest.ini`.
* For any tool that genuinely cannot read an external file, CI recomputes a **semantic**
  hash of the relevant parsed sections and compares it to `.quality/config-hashes.json` —
  which is itself a protected path.

Semantic, not byte-level. A raw `sha256sum` over grepped lines trips on reformatting, key
reordering, comment edits and whitespace, producing integrity failures for changes that
altered no policy. Agents reformat files constantly, so this would fire weekly and be
disabled within a month. Required procedure:

```text
parse TOML
  ↓ extract the protected subtree (e.g. tool.ruff)
  ↓ drop comments; normalize scalar types (1.0 and 1 differ, "1" does not equal 1)
  ↓ sort every mapping key recursively; preserve array order, which is significant
  ↓ serialize canonically (sorted-key JSON, no whitespace)
  ↓ SHA-256
```

Array order is preserved deliberately — for `select` and `extend-select` the set matters,
not the order, but for `per-file-ignores` and path-ordered options it can matter, and
sorting away a real difference is worse than hashing a cosmetic one.

This path exists as a fallback. If everything lives in `.quality/`, no section hashing is
needed at all, which is the better outcome.

### 11.3 The check

Runs before every other gate. Any modification to a protected path without the §11.1
approval → **fail**, and the failure is not overridable by the allowlist.

Legitimate guardrail changes go in their own approved PR, reviewed on their own merits,
never bundled with the feature that motivated them.

---

## 12. Pyright strictness on a legacy repo

* `typeCheckingMode = "basic"` repo-wide. Two gates, and both are needed:
  * `quality-fast` — Pyright over the changed-file list, zero errors. Fast feedback.
  * `quality-full` — Pyright over the **whole repository**, per-rule ratchet against the
    merge base (§4.3).

The repo-wide run is the authoritative one. Changed-file scoping is an approximation for
speed and must never be the correctness boundary: narrowing a return type in `users.py`
from `User` to `User | None` breaks unchanged consumers in `billing.py`, `notifications.py`
and `api.py`, and a changed-file run sees none of them. The per-rule ratchet catches this
precisely, because the break lands in a specific bucket — `reportOptionalMemberAccess`
jumps from 0 to 3 — rather than being absorbed into a total that an unrelated fix could
offset.
* `strict` applied per-directory via an explicit list in `pyrightconfig.json`, starting
  with whatever is already clean.
* A new top-level module under `src/` must be added to the strict list in the same PR;
  enforced by a CI check. New directory absent from the list → FAIL.
* The strict list only grows. Removing an entry is a protected-path change.

Do **not** enable strict repo-wide and baseline the resulting thousands of errors. A
baseline that large is unreviewable, defeats fingerprint matching, and hides real
regressions in noise.

---

## 13. Design review

Deterministic gates cannot catch the category that matters most: unnecessary abstraction,
premature generalisation, a factory with one implementation, a parallel implementation of
a concept that already exists, wrong domain boundaries.

v2 contradicted itself — §13 called the fresh-context reviewer "required," §19 filed it
under Tier 3. Resolved as follows, in the direction of honesty about what is reliable:

**Required at every tier:** a human reads the diff before merge. This is not optional at
Tier 1, and no amount of green gates substitutes for it.

**Optional (Tier 3):** a fresh-context agent reviewer that assists the human. If used it
MUST receive only the diff, the checklist, and the repository conventions doc — never the
implementation conversation. Same context and same priors produce the same blind spots, so
a reviewer that inherits the implementation transcript is close to worthless. It outputs
findings; it never edits code; its output is advisory input to the human, not a gate.

Checklist for either reviewer:

```text
Could this be materially simpler without losing required behaviour?
Does an abstraction here have exactly one implementation?
Does this duplicate a concept that already exists under a different name?
Was anything left behind by the refactor that is now unreachable?
Is there configuration for something that has never varied?
Does the change touch files unrelated to the stated task?
Do the new tests fail if the implementation is wrong?
```

The last question is where a human's attention is worth most. Nothing upstream answers it.

---

## 14. Command surface

```text
quality-fix        MAY mutate. Ruff --fix, Ruff format. Never in validation or CI.
quality-fast       Read-only. Diff-scoped only. < 15s.
quality-full       Read-only. Whole repo, ratchets included. Budgets per §5.
quality-baseline   Records accepted state. Tier 2. Human-invoked only.
quality-diff       Fingerprinted comparison to baseline. Tier 2.
```

`quality-baseline` refuses to run with a dirty working tree or off the default branch.
v2 additionally proposed detecting agent sessions via an environment marker — **dropped**,
since an agent can unset an environment variable. `.quality/baseline.json` is a protected
path under §11 instead, so a regenerated baseline cannot merge without human approval.
That is the real control; the env check would have been theatre.

---

## 15. Machine-readable output

```json
{
  "status": "fail",
  "tier": 1,
  "base": {"source": "ci", "ref": "refs/heads/main", "sha": "abc123def456"},
  "dependency_environment_changed": false,
  "exempt_tools": [],
  "gates": {
    "integrity":   {"status": "pass", "mechanism": "absolute"},
    "ruff":        {"status": "pass", "mechanism": "diff-scoped"},
    "ruff-s":      {"status": "pass", "mechanism": "diff-scoped"},
    "pyright":     {
      "status": "fail",
      "mechanism": "per-rule-ratchet",
      "regressed_rules": [
        {"rule": "reportOptionalMemberAccess", "base": 0, "head": 3},
        {"rule": "<no-rule>", "base": 1, "head": 2}
      ],
      "totals": {"base": 41, "head": 44}
    },
    "pytest":      {"status": "pass", "mechanism": "absolute"},
    "semgrep":     {"status": "pass", "mechanism": "diff-scoped"},
    "complexity":  {"status": "fail", "mechanism": "diff-scoped"},
    "cycles":      {"status": "pass", "mechanism": "absolute"},
    "pip-audit":   {"status": "pass", "mechanism": "absolute"}
  },
  "findings": [
    {
      "gate": "complexity",
      "rule": "ruff/C901",
      "path": "src/billing/service.py",
      "symbol": "billing.service.process_request",
      "line": 142,
      "severity": "error",
      "detail": "complexity 14 exceeds threshold 10"
    }
  ],
  "metrics": {
    "sloc": {"base": 6421, "head": 6810},
    "avg_complexity": {"base": 3.7, "head": 3.9},
    "coverage": {"base": 88.4, "head": 89.1},
    "diff_coverage": 72.5
  },
  "allowlist_expiring_within_30d": ["a3f9c21e"]
}
```

Requirements: `base.sha` is always present and is the commit every ratchet actually compared
against; `exempt_tools` lists any analyzer whose ratchet was skipped this run, with
`dependency_environment_changed` explaining why when that is the cause — an exemption that is
not visible in the report is indistinguishable from a pass; every gate reports its mechanism,
so a reader can tell an absolute failure from a ratchet slipping; per-rule ratchet failures
name every regressed rule with both integers, and report totals separately so a reader can
see when totals fell while a specific rule rose; every failure carries an actionable
`detail`; Tier 2 adds `introduced` / `resolved` / `unchanged` arrays.

Exit codes: `0` pass, `1` hard gate, `2` regression only, `3` tooling error. Distinct codes
matter — an agent must be able to tell "I broke something" from "the linter crashed," or it
will confidently fix the wrong thing.

---

## 16. Agent workflow (AGENTS.md content)

```text
1. Read repository conventions.
2. Understand the change. Inspect existing patterns before writing new ones.
3. Implement the smallest complete solution.
4. Run quality-fast. Fix failures. Repeat until clean.
5. Run quality-full.
6. If a ratchet failed, find and fix what you added. Do not offset it with an
   unrelated fix.
7. Stop. Report status. Do not self-approve.
```

Never, under any circumstances:

* disable, skip, or xfail a test to make a task complete
* add a suppression comment
* edit anything under §11.2's protected paths
* regenerate the baseline
* add or edit an allowlist entry
* weaken a threshold, coverage target, or lint rule
* leave the previous implementation in place after replacing it
* create a parallel implementation where an existing one should be extended
* modify files unrelated to the task

If a gate appears wrong, **stop and report it**. Do not work around it. A task that
requires weakening its own evaluator is a task that needs a human.

---

## 17. Installer

An agent merging config into `pyproject.toml`, CI workflows and `AGENTS.md` is a delicate
edit on files it is otherwise forbidden to touch, performed by the tool the system exists
to distrust. Instead:

* All configuration goes in `.quality/` and dedicated dotfiles. Nothing is written into
  `pyproject.toml` (§11.2), which also makes protection expressible.
* Where a tool can only read `pyproject.toml`, the installer **prints** the block to paste
  and records its hash in `.quality/config-hashes.json`.
* The installer opens a PR. A human merges it. It never commits to the default branch.
* `AGENTS.md` gets an appended, clearly delimited section. Nothing existing is rewritten.

**Two distinct roles, to remove an apparent contradiction with §16.** The installer
generates the day-one allowlist; implementation agents are forbidden from touching it. Both
are true because they are different roles at different times:

```text
Bootstrap installer, pre-activation:
    MAY propose protected configuration, the toolchain lock, and initial
    exceptions — all inside the installation PR.
    None of it takes effect until independently approved and merged (§11.1).

Implementation agent, post-activation:
    MUST NOT modify protected configuration or exceptions, ever.
```

The installer has no special privilege. Its output is a proposal reviewed by a human like
any other guardrail change; it is merely the first one.

Phases: analyse repo → propose plan → add dev dependencies → write `.quality/` → capture
Tier 1 state and generate the day-one allowlist → wire commands into the existing task
runner → append AGENTS.md → add CI job → run self-tests.

---

## 18. Self-tests

The guardrail system MUST be tested against deliberately broken inputs, in its own CI.
Untested guardrails stop working silently, and they fail open.

Each case: apply the violation to a scratch copy, assert the expected gate fails with the
expected mechanism, assert `quality-full` on the clean copy passes.

```text
formatting violation                → Ruff, diff-scoped
type error in a changed file        → Pyright, diff-scoped
type error caused in another file   → Pyright, per-rule ratchet
fix one F401, add one B006          → Ruff, per-rule ratchet (a total ratchet passes this)
pyright syntax error, rule = null   → per-rule ratchet, "<no-rule>" bucket
approved PR enabling a new rule     → that tool exempt; other tools still ratchet
toolchain.lock bumps Ruff           → Ruff exempt, Pyright/deptry still ratchet
project lockfile changes            → Pyright/deptry exempt, flagged in report;
                                      Ruff/pyscn still ratchet
BASE analysed with HEAD deps        → must NOT happen; assert base run used base lockfile
shallow clone in CI                 → exit 3, not a silent pass
unresolvable base ref               → exit 3, not a silent pass
allowlisted legacy cycle            → cycles gate passes; a second cycle fails it
cycle reported from another member  → canonicalisation matches the same allowlist entry
complexity 15 function              → Ruff C901, diff-scoped
# type: ignore on an added line     → added-line scan / PGH003
@pytest.mark.skip added             → Semgrep, diff-scoped
test with no verification act       → Semgrep, diff-scoped
test asserting on a MagicMock       → Semgrep, diff-scoped
eval() added                        → Ruff S307, diff-scoped
circular import introduced          → pyscn, absolute
unreachable function added          → pyscn, ratchet
vulnerable dependency pinned        → pip-audit, absolute
edit to .quality/config.toml        → integrity check
expired allowlist entry             → integrity check
allowlist entry added at all        → integrity check (regardless of added_by value)
pyproject reformatted, no policy    → integrity check does NOT fire (semantic hash)
  change
baseline regenerated in a PR        → integrity check
duplicated 40-line block            → quality-diff regression (Tier 2)
symbol renamed, no other change     → quality-diff reports 1 introduced + 1 resolved
                                      and FAILS, requiring review (Tier 2, §7)
```

The integrity cases and the rename case matter most. They test that the system resists what
it was built to resist, and that it does not cry wolf — rather than testing that linters
lint.

---

## 19. Acceptance criteria

**Tier 1 — ship this first:**

* [ ] `quality-fast` read-only, diff-scoped, measured under 15 s
* [ ] `quality-full` read-only; static-analysis overhead within the §5 SHOULD, project test
      budget declared in config
* [ ] `quality-fix` exists and is excluded from CI
* [ ] Ruff configured: lint, format, `C901`, enumerated `S` list, `PGH`, `PT`, `ERA`
* [ ] Pyright `basic` repo-wide, strict directory list present, new-module check active
* [ ] pytest with branch coverage in the gate
* [ ] Semgrep ruleset: suppressions, test disabling, verification-act absence, trivial
      asserts, MagicMock truthiness
* [ ] circular imports gated via structured pyscn analysis minus allowlist (§10.1), zero
      unallowlisted cycles remaining — **not** raw `pyscn check --max-cycles 0`
* [ ] pip-audit gating **all** findings regardless of severity
* [ ] per-rule ratchets on Ruff, Pyright, pyscn and deptry, with a `<no-rule>` bucket
* [ ] `.quality/toolchain.lock` pinning exact versions; analyzer versions always come from
      HEAD, never from the BASE worktree
* [ ] project dependencies resolved per commit for environment-sensitive analyzers; envs
      cached by lockfile hash and shared when the lock is unchanged
* [ ] Pyright and deptry ratchets exempted, and flagged in the report, when the project
      lockfile differs between BASE and HEAD
* [ ] exemption is tool-level by default; any rule-level narrowing is explicitly opted into
* [ ] base-ref resolution per §4.6, failing closed with exit 3; shallow-clone check present;
      CI checkout uses `fetch-depth: 0`; resolved SHA recorded in the report
* [ ] each gate's scope declared as file / line / function, implemented via one shared
      range-intersection filter
* [ ] C901 scope verified against Ruff's actual `end_location`, and the policy text matches
      whatever was implemented
* [ ] allowlist adapters for pip-audit and cycles, with cycle canonicalisation
* [ ] merge-base tool runs cached by SHA; base counts computed, never committed
* [ ] `.quality/allowlist.toml` with expiry enforcement; `added_by` documented as
      provenance only
* [ ] no quality configuration remains in `pyproject.toml`; any residual section hashed
      semantically, not byte-wise
* [ ] deployment model from §11.1 chosen and written down, including model C's admission
      that the check is a tripwire rather than a control
* [ ] integrity check uses credential isolation — **not** a PR label
* [ ] AGENTS.md workflow section appended
* [ ] every self-test in §18 marked Tier 1 passing
* [ ] CI runs the full gate, read-only, never commits
* [ ] a human reviews every diff before merge (§13)

**Tier 2 — after Tier 1 has been in daily use:**

* [ ] §7 fingerprint scheme, unit-tested against a rename, an inserted line, and a moved file
* [ ] `quality-baseline`, human-invoked, refuses dirty tree, protected path
* [ ] `quality-diff` emitting `introduced` / `resolved` / `unchanged` / `regressions`
* [ ] ratchets upgraded to identity-matched comparison
* [ ] regression thresholds from §8 configurable
* [ ] injected regression detected end to end; pure rename surfaces as introduced +
      resolved and is cleared by review, not by a rename detector

**Tier 3 — optional:**

* [ ] architecture tests reflecting real layers, not a placeholder
* [ ] `diff-cover` on changed lines
* [ ] `mutmut` on changed files at ≥ 60% killed
* [ ] fresh-context design reviewer per §13

---

## 20. Operating principle

Make the path of least resistance for an agent also the path toward maintainable code.

Three limits, stated so nobody mistakes this for more than it is:

**Syntactic gates get routed around, not internalised.** Ban a pattern and an agent writes
a longer construction with the same behaviour that no rule matches. Every gate here is
worth having; none of them teaches the model anything.

**Tier 1 ratchets still net out within a rule.** Per-rule grouping stops you trading an
`F401` for a `B006`, but two `F401`s in different files still swap cleanly. That is the
price of skipping fingerprinting, and it is worth paying to get something running in a day.

**Integrity protection is only as strong as credential isolation.** In the common case of
an agent running in your own shell, there is no local control — only a server-side review
boundary involving a second person, and for a solo developer not even that. The check is
still worth having as a tripwire; it is not a security boundary, and §11.1 says which one
you actually have.

**Nothing here evaluates whether the right thing was built.** Every gate can pass on code
that solves the wrong problem, correctly, with excellent coverage of the wrong behaviour.
That stays a human's job, and this system is worth building only to the extent it frees up
human attention for exactly that.
