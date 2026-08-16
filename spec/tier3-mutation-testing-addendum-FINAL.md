# Addendum to v5.1: Tier 3 — Mutation Testing (final)

**Status:** frozen. The v5.1 Tier 1 specification remains frozen; this expands §9.3 and adds
a Tier 3 gate. Do not implement before Tier 1 has been in daily use.

**v3.1 → final — clerical only, no content change:**

* Report example corrected: `no_tests: 3` now shows `no_tests_flagged: true`, matching the
  rule in §4. Examples become de facto implementation tests, so a contradictory one is worse
  than none.
* Section numbering normalised: `3a → 3.1`, `4b → 4.1`, `4a → 4.2`, and the outcome-model
  section now precedes the threshold section it feeds.

**v3 → v3.1 — contract clarifications, no redesign:**

1. **How the adapter obtains the outcome model** (§4.1). `mutmut export-cicd-stats` does not
   export every state v3's normalisation table requires. Adds a conditional contract so most
   repositories can use the public export, and states when internals coupling is unavoidable.
2. **"Large `no_tests`" defined** as `> 0` (§4). v3 reintroduced exactly the undefined
   "materially" language the main spec removed in its own v3.
3. **Anti-evasion invariant reworded** (§6). "Only by strengthening a test" wrongly forbade
   legitimately simplifying production code, which is a valid response to a survivor.
4. **Evaluation status separated from CI enforcement mode** (§7, §8). Report-only must not
   swallow tooling errors — a broken runner stays broken during the observation period.
5. Deterministic sampling algorithm specified rather than left to a PRNG (§3).

**v2 → v3 errata:**

1. **The cap is on functions, backstopped by wall clock** (§3). v2 said "enumerate candidate
   mutants, sample to 150, execute" — but mutmut exposes no documented command that lists
   mutants without running them, so the sampling step had nowhere to happen. Exact-mutant
   capping is retained as a documented escape hatch with its coupling cost stated.
2. **Outcome vocabulary matched to mutmut 3's actual states** (§4), and `no tests` moved
   into the denominator. v2 excluded it, which hid the purest instance of the property being
   measured.
3. **Configuration key names corrected to mutmut 3 and marked non-normative** (§6). v2 listed
   2.x-era names; the protected object is the whole `[tool.mutmut]` subtree either way.
4. Symbol-to-target name translation self-tested (§9).

**v1 → v2 corrections**, all of which were wrong about how mutmut 3 actually works:

1. **Scope is changed functions, not post-filtered changed lines** (§2). mutmut supports
   module- and function-targeted runs via mutant-name patterns; there is no Git-diff
   selector, but there is native targeting.
2. **Selection and sampling happen before execution** (§3). v1's cap was applied to results
   after the run, which saves no runtime at all — the design error in v1.
3. **Test selection is mutmut's, not ours** (§3). v1 said to build coverage-guided test
   selection. mutmut already does relevant-test selection natively; coverage belongs to
   *candidate* filtering via `mutate_only_covered_lines`. Two different optimisations.
4. **Score denominator normalised across outcome classes** (§4.2). killed / timeout /
   survived / suspicious / skipped are not interchangeable and v1 left the ratio undefined.
5. **Config protection routes through §11.2 semantic hashing** (§6). mutmut configures in
   `pyproject.toml` or `setup.cfg`; there is no standalone file to protect wholesale.
6. **Platform capability reported explicitly** (§3.1). mutmut 3 needs `fork`; Windows needs
   WSL. An unsupported runner must not report `skipped`.

---

## 1. What this closes

Every other gate in the system can pass on this:

```python
def test_discount():
    result = calculate_discount(order)
    assert result is not None
```

pytest green, coverage counted, Ruff clean, Pyright clean, §9's static checks satisfied —
there is a real assertion on a real call, so nothing upstream objects. The test proves
almost nothing.

Mutation testing asks the one question nothing else in the stack asks:

> If the implementation were subtly wrong, would the tests notice?

The output is also unusually good agent feedback, because it is specific rather than
statistical:

```text
SURVIVED  src/billing/discount.py:42  calculate_discount
          if total > threshold:  →  if total >= threshold:
```

A coverage percentage tells an agent nothing actionable. That tells it exactly which
behavioural boundary is untested.

Layered against the rest of the stack:

```text
Ruff / Semgrep  →  reject obviously fake tests (§9)
pytest          →  verify stated expected behaviour
coverage        →  verify the code was executed at all
mutation        →  verify the tests react to plausible faults
```

Coverage and mutation score answer different questions and MUST remain separate signals.
Neither is folded into an aggregate.

---

## 2. Scope — changed functions

> mutmut has no native Git-diff selector. It **does** support module- and function-targeted
> runs via mutant-name patterns:
>
> ```bash
> mutmut run "my_module*"
> mutmut run "my_module.my_function*"
> ```
>
> So the wrapper's job is translating changed functions into run targets — not running whole
> files and sorting the wreckage afterwards.

v1 specified changed **lines**, post-filtered from a whole-file run. That is both harder
(enumerating mutants without executing them is not a documented workflow) and pointless
(§3 — the filtering happens after the runtime is already spent).

```text
git diff vs resolved base SHA
    ↓ identify changed production functions          (excludes tests/, generated/, docs)
    ↓ translate symbols into mutmut target patterns  (§9 — self-test this)
    ↓ deterministically sample down to the function cap  (§3)
    ↓ mutmut run <selected target patterns>
```

**Accepted coarseness.** Touch one line in a forty-line function and the whole function is
mutated, so mutants in lines you did not write count toward your score. This is the price of
a selector that exists. It is also defensible: if you modified a function, its tests are your
concern. State it in the report rather than letting people discover it from a confusing
number.

**This needs the diff→AST mapper Tier 1 declined.** v5.1 §4.2 explicitly says not to build a
diff→AST mapping layer for `C901` in Tier 1, and to accept changed-file scope instead. Mapping
a diff to changed functions is exactly that layer. Tier 3 is allowed to be more expensive, so
build it here — and once it exists, **`C901` can be retroactively upgraded from changed-file
to true changed-function scope**. Reuse the work; do not write it twice.

Report the scope honestly:

```text
"N changed production functions were mutation-tested"
```

not "all changed logic was verified."

Skip the job entirely, with a `skipped` status rather than a pass, when there are no eligible
changed functions: docs-only, tests-only, generated-code-only changes.

---

## 3. Runtime — the real constraint

Mutation testing runs the test suite once per mutant. A 30-second suite with 200 eligible
mutants is roughly 100 minutes serially. This is the reason mutation testing gets adopted and
then quietly deleted, and the threshold debate is irrelevant next to it.

**Selection precedes execution.** This was v1's design error: it capped at 150 mutants *after*
the run, which saves exactly nothing if mutmut already executed 900. The cap is a runtime
control and must be applied to the target set, not the result set.

```text
✗ run everything → filter results → cap        (v1: cap saves no time)
✓ select targets → cap targets → run           (v2)
```

Mandatory:

* **Candidate filtering via coverage.** Set `mutate_only_covered_lines = true` so uncovered
  source lines never generate mutants at all. Uncovered code produces guaranteed survivors
  that tell you nothing mutation-specific — coverage already told you.
* **Native test selection.** mutmut 3 works out which tests are relevant to a mutant itself.
  **Do not build a coverage→test mapping.** v1 said to, and an implementer following that
  would rebuild machinery that already exists. Tune `max_stack_depth` if repository behaviour
  warrants it; otherwise leave it alone.
* **A function cap plus a wall-clock backstop.** v2 said "cap at 150 mutants," which requires
  enumerating mutants before running them — and **mutmut exposes no documented command that
  lists mutant IDs without executing them.** The sampling step had nowhere to happen. Two
  honest options:

  **Chosen — cap functions, backstop with time.**

  ```text
  function cap        default 25 changed functions per PR
  sampling            deterministic, see below
  wall-clock limit    default 20 min on the CI job
  on exceeding time   report truncated: true, exit 3 (tooling-inconclusive), never pass
  ```

  **Sampling algorithm.** Do not use `random.Random(base_sha).sample(...)` — CPython's PRNG
  algorithm is an implementation detail, not a stability guarantee, and a runtime change would
  silently reshuffle which functions get tested. Use:

  ```text
  eligible functions
    ↓ canonical qualified names
    ↓ sort lexicographically
    ↓ key each by SHA-256(base_sha | symbol)
    ↓ sort by that key
    ↓ take first N
  ```

  Same base, same eligible set, same selection — across Python versions and implementations.

  Function targeting is the documented public interface (`mutmut run "module.function*"`), so
  this needs no coupling to mutmut internals. Its weakness is that one large function can
  generate many mutants and blow the budget — which the wall-clock limit contains, and which
  produces a concrete data point rather than a silent overrun.

  **Escape hatch — exact mutant IDs.** If the function cap plus time limit proves inadequate
  in practice, read mutmut's generated mutant metadata, sample exact IDs, and pass them to
  `mutmut run`. This gives a true runtime cap and is defensible given the toolchain is pinned,
  **but the adapter is deliberately coupled to an undocumented internal model of the pinned
  mutmut version.** If built, it MUST carry compatibility self-tests that fail loudly on a
  version bump. Do not build it speculatively — a silent drop in mutants tested is worse than
  an overrun that announces itself.

  **Verify first.** Whether a public enumeration path exists in the pinned version is the
  single assumption in this document most likely to be wrong. Check it before writing the
  wrapper.

* **Parallelism** across available CI cores.
* **Its own CI job**, in parallel with `quality-full`. Never in `quality-fast`, never in
  pre-commit, never in the inner agent loop. Excluded from v5.1 §5's budgets by design.

If the job becomes too slow, narrow the scope — function cap, selection — before touching the
threshold. Weakening the threshold makes the gate meaningless; narrowing scope makes it
smaller but still true.

---

## 3.1 Platform capability

mutmut 3 requires an OS with `fork`. On Windows it must run under WSL.

The gate MUST report platform capability as a distinct state:

```text
supported        → run normally
unsupported      → TOOLING ERROR, exit 3
tooling-error    → exit 3
```

**An unsupported runner MUST NOT report `skipped`.** A docs-only PR is legitimately skipped; a
Windows runner that cannot execute mutmut is a configuration failure, and collapsing the two
turns a broken gate into a green one. This is the same fail-open pattern as v5.1's
shallow-clone case, and it gets the same treatment: exit 3, say why.

The installer MUST document the required CI environment rather than discovering this in
production.

---

## 4. Score normalisation

v1 wrote `score = killed / eligible` and left "eligible" undefined. mutmut 3 distinguishes
more states than v2 accounted for, and they are not interchangeable; an undefined denominator
means the gate measures something slightly different on every run.

Policy for a **test-quality** metric — which is deliberately not the same as mutmut's own
badge formula, and that is fine as long as the contract is written down:

```text
state                    numerator   denominator   notes
──────────────────────   ─────────   ───────────   ────────────────────────────────
killed                       yes          yes
timeout                      yes          yes      flagged separately, see below
survived                      no          yes
no tests                      no          yes      see below — v2 got this wrong
suspicious                    no           no      excluded, reported
skipped                       no           no      excluded, reported
caught by type check          no           no      excluded, reported as a positive
segfault                      no          yes      conservative default
not checked                  —            —        incomplete run → exit 3
interrupted                  —            —        incomplete run → exit 3
```

```text
scoreable        = killed + timeout + survived + no_tests + segfault
effective_killed = killed + timeout
score            = effective_killed / scoreable
```

**`no tests` belongs in the denominator.** v2 excluded it. That was backwards: a mutant with no
relevant test is the purest possible instance of the thing mutation testing exists to expose,
and excluding it means the score improves the less of your code is tested.

**`no tests` should also be rare, and isn't free information when it isn't.** With
`mutate_only_covered_lines = true`, uncovered lines never generate mutants, so any `no_tests`
count means coverage data and mutmut's test selection disagree about what is actually
exercised.

**Threshold: `no_tests > 0` → flag.** Warning, not failure. v3 said "large `no_tests` count →
flagged" without defining large, which is precisely the undefined "materially" language the
main specification removed from itself. Since the count should be zero under
`mutate_only_covered_lines`, any non-zero value is worth surfacing:

```json
{"no_tests": 3, "no_tests_flagged": true,
 "detail": "3 covered mutants had no relevant tests according to mutmut"}
```

If real use shows this is noisy, add a configurable ratio then. Do not invent a second
threshold now.

**`caught by type check` is excluded, and reported as a positive.** The type checker caught the
fault, which is good; it just isn't evidence about the tests, which is what this number
measures.

**`not checked` and `interrupted` are tooling errors, not results.** An incomplete run must not
produce a score. Exit 3, consistent with every other fail-closed case in v5.1.

**Timeout inflation guard.** A mutant causing non-termination counts as killed without any test
having detected wrong *behaviour* — it detected non-termination. Usually harmless, but a suite
with many timeouts has an inflated score, and the cause is normally a test timeout set too low
rather than strong tests. Report the timeout count, and **flag when timeouts exceed 20% of
`scoreable`** so the number is read with suspicion rather than pride.

Confirm the state vocabulary against the pinned mutmut version during implementation. The
requirement is that the mapping is written down in the adapter and self-tested, not that this
exact list survives a version bump.

---

## 4.1 Obtaining the outcome model

> `mutmut export-cicd-stats` does **not** export every state the table above requires.

The obvious implementation — call `export-cicd-stats`, parse the JSON, normalise — hits a wall.
The export carries killed, survived, total, no-tests, skipped, suspicious, timeout, interrupted
and segfault, but omits at least `caught_by_type_check` and `not_checked`. An implementer
following §4 literally will discover the contract is unimplementable from the public export.

**Make the contract conditional on what the configuration can actually produce.** A state that
cannot occur does not need accounting:

```text
type_check_command NOT configured
    → caught_by_type_check cannot occur
    → export-cicd-stats is sufficient
    → no internals coupling

type_check_command configured
    → caught_by_type_check can occur and is not exported
    → adapter MUST read mutmut's generated metadata / programmatic state
    → coupling is deliberate and MUST carry compatibility self-tests
       that fail loudly on a version bump
```

Most repositories will not set `type_check_command`, so most adapters need no internals access
at all. Prefer that path; it is the difference between an adapter that survives a mutmut
upgrade and one that quietly miscounts after it.

`not_checked` is handled the same way: if the run completes, it does not occur; if the run does
not complete, exit 3 without producing a score (§4), so its absence from the export is
irrelevant.

**Note the consequence for §3's escape hatch.** If a repository already needs internals access
for outcomes, exact-mutant-ID capping stops being a *new* category of coupling and becomes a
marginal extension of coupling already present. The preference for the function cap still
holds — it is simpler and one fewer thing to revalidate — but the argument against the escape
hatch is weaker for those repositories than §3 implies.

---

## 4.2 Threshold, and the small-denominator problem

```text
score = effective_killed / scoreable        (as defined in §4)
```

Default floor: **60%**, configurable in `.quality/config.toml`.

```text
< 60      fail
60–75     adoption floor
75–90     healthy
> 90      strong — inspect for tests coupled to implementation detail
```

**Minimum denominator: `scoreable` >= 20.** Below that, report the score and the survivors
but do **not** fail the gate.

This rule is not optional. A PR with four eligible mutants and two survivors scores 50% and
fails, having done nothing wrong — small diffs produce wildly unstable ratios. Random
failures are precisely how a gate loses credibility, after which agents and humans alike
learn to click through it.

Do not target 100%. Equivalent mutants exist — mutations that produce behaviourally
identical code — and chasing them produces brittle tests, not better ones.

---

## 5. No allowlist integration in v1

The natural instinct is to route equivalent mutants through `.quality/allowlist.toml`.
**Don't**, for two reasons that only became visible on contact with this gate.

**Equivalent mutants are not debt.** v5's allowlist is built around expiry, because every
other exception it holds is something that should eventually be fixed. An equivalent mutant
is a permanent property of the code; there is nothing to fix. Forcing a 180-day expiry means
re-adjudicating the same mutant forever, and that toil is what gets the whole job disabled.

**Mutant identity is unstable.** mutmut's mutant naming shifts when the surrounding file
changes, so entries decay exactly the way Tier 2 fingerprints decay — the identity problem
v5 spent a whole section confining to Tier 2, reappearing in Tier 3.

The threshold already absorbs equivalent mutants. That is what a floor below 100% is for.
Set it low enough and skip the identity problem entirely.

Revisit only if real usage shows a specific function whose equivalent mutants repeatedly
drag an otherwise well-tested change below the floor. At that point the answer is a
`permanent = true` entry class restricted to `rule = "mutation/equivalent"`, keyed on
`(path, symbol, mutation_description)` and accepting drift — not a general relaxation of §10.

---

## 6. Anti-evasion

Adding a gate adds a bypass surface. This one has two.

**Suppression pragmas.** Add to §9's added-line scan, gated at zero:

```python
# pragma: no mutate
```

plus any block or region form the installed mutmut version supports. Same treatment as
`# type: ignore` — it is a suppression comment and belongs in the same list.

**Configuration.** The entire mutmut configuration is guardrail policy under v5.1 §11.2, not
merely the exclusion list. Result-affecting settings in mutmut 3 include:

```text
source_paths                        narrow the target until the failure is outside it
only_mutate                         same
do_not_mutate                       exclude the specific file or function
do_not_mutate_patterns              same, by pattern
pytest_add_cli_args_test_selection  point mutation at a trivial subset of the suite
pytest_add_cli_args                 same
also_copy                           change what the mutation run can see
max_stack_depth                     change which tests count as relevant
mutate_only_covered_lines           interacts with coverage config to shrink candidates
type_check_command                  shift kills into the excluded type-check bucket
timeout settings                    shift survivors into the timeout bucket
cache / dependency-change controls  suppress re-evaluation entirely
```

**This list is explanatory, not normative.** It reflects the pinned mutmut 3 vocabulary and
will drift; v2 listed `paths_to_mutate`, `runner` and `tests_dir`, which are 2.x names. The
protected object is the **entire `[tool.mutmut]` semantic subtree**, never a hand-maintained
key denylist — a denylist fails open on exactly the key someone added after it was written.

**There is no standalone mutmut config file to protect wholesale.** mutmut configures in
`[tool.mutmut]` in `pyproject.toml` or in `setup.cfg`. v1 said "protect the entire config
file" as though such a file exists. Route it through the mechanism v5.1 already built for
exactly this case:

```text
config in pyproject.toml / setup.cfg:
    parse the mutmut section
    canonicalize per §11.2 (sort keys, normalize scalars, preserve array order)
    semantic-hash it
    record the expected hash in .quality/config-hashes.json  (protected)

if a pinned mutmut version later ships a standalone config file:
    prefer that file and protect the path wholesale
```

No special case. This is what §11.2's fallback path was for, and mutation testing is its first
real user.

Invariant:

> An implementation agent MUST NOT make a failing mutation result pass by weakening,
> narrowing, excluding, suppressing or otherwise manipulating the evaluator. It MAY resolve a
> survivor by strengthening behavioural tests, or by legitimately changing or simplifying
> production code.

v3 said "by any means other than strengthening a test," which was wrong. Simplifying an
implementation so the mutation opportunity no longer exists — deleting dead branching,
delegating to a library, correcting genuinely wrong behaviour — is a valid and often better
response to a survivor than adding a test, and the design review checklist (v5.1 §13) actively
wants it.

**The obvious objection, stated rather than hidden:** deleting the branch that generated the
survivor is *also* how you evade this gate. What prevents that is not the mutation gate but the
rest of the stack — the tests must still pass, so tested behaviour cannot be removed. Untested
behaviour can be, which is the original problem restated, and it is why v5.1 §13 requires a
human on the diff. This invariant is not airtight on its own and should not be read as if it
were.

---

## 7. Reporting

Survivors matter more than the percentage. The score tells you whether to fail; the survivor
list tells the agent what to do.

```json
{
  "gate": "mutation",
  "status": "fail",
  "enforcement": "report-only",
  "would_block": true,
  "platform": "supported",
  "score": 0.58,
  "threshold": 0.60,
  "outcomes": {
    "killed": 22, "timeout": 2, "survived": 14, "no_tests": 3,
    "segfault": 0, "suspicious": 1, "skipped": 0, "caught_by_type_check": 2
  },
  "scoreable": 41,
  "effective_killed": 24,
  "timeout_share_flagged": false,
  "no_tests_flagged": true,
  "functions_tested": 6,
  "functions_eligible": 6,
  "sampled": false,
  "truncated": false,
  "survived_mutants": [
    {
      "path": "src/billing/discount.py",
      "symbol": "billing.discount.calculate_discount",
      "line": 42,
      "mutation": "comparison > changed to >="
    }
  ]
}
```

The full `outcomes` breakdown is reported, not just the ratio, so a reader can see when a
score is riding on a tiny `scoreable`, propped up by timeouts, or dragged by a `no_tests` count
that signals a coverage misconfiguration. `functions_tested` against `functions_eligible`
states the scope actually covered; `truncated` means the wall-clock backstop fired and the
result is inconclusive, not passing.

**`status` and `enforcement` are independent.** `status` is what the evaluation found;
`enforcement` is what CI does about it. Keeping them separate is what lets §8's report-only
period exist without the self-tests contradicting it:

```text
enforcement = report-only
    threshold failure    status "fail",          would_block true,   CI exit 0
    tooling failure      status "tooling-error", would_block true,   CI exit 3

enforcement = blocking
    threshold failure    status "fail",                              CI exit 1
    tooling failure      status "tooling-error",                     CI exit 3
```

**Report-only must not become fail-open.** Only the *quality threshold* is non-blocking during
the observation period. An unsupported platform, an interrupted run, unreadable mutant
metadata, or a tripped wall-clock backstop still exit 3 from day one — otherwise four weeks of
green reports could mean four weeks of a gate that never ran.

Command surface gains one entry:

```text
quality-fast       < 15s, inner loop
quality-full       whole repo, ratchets
quality-mutation   dedicated CI job, changed production code only
```

---

## 8. Rollout — report-only first

**Run `quality-mutation` in report-only mode for the first four weeks.** Emit the score and
survivors, do not fail the build.

Mutation testing pushes toward brittle tests, and it pushes agents harder than it pushes
humans. Told to kill a surviving mutant, an agent will frequently write a test asserting the
exact internal behaviour it observed — which kills the mutant, couples the test to the
implementation, and turns the next legitimate refactor into a failing suite. That cost is
real and it starts well below the 90% mark where over-fitting is usually flagged.

Four weeks of report-only tells you what the agent actually writes in response to a survivor
before you make it mandatory. If the answer is boundary tests, turn the gate on. If the
answer is assertions on internals, the threshold needs to be lower, or the gate stays
advisory to a human and never becomes a gate at all. Both are acceptable outcomes; guessing
in advance is not.

---

## 9. Self-tests

Add to §18. This is the case that demonstrates the property the gate exists for:

```python
# fixture
def is_adult(age: int) -> bool:
    return age >= 18

# weak test
def test_adult():
    assert is_adult(20)
```

The mutation `age >= 18` → `age > 18` survives, because 18 itself is never tested.

```text
weak test present                    → quality-mutation FAIL, boundary mutant reported
add test_exact_adult_boundary(18)    → quality-mutation PASS
# pragma: no mutate on added line    → suppression scan FAIL (single, block and
                                       start/end forms all covered)
edit to [tool.mutmut] in pyproject   → integrity check FAIL via semantic section hash
reformat pyproject, no mutmut change → integrity check does NOT fire
scoreable = 6, 3 survive             → reported, does NOT fail (min denominator)
mutant with no relevant test         → counts in denominator, not numerator
any no_tests > 0                     → no_tests_flagged true, warning not failure
"not checked" / interrupted run      → exit 3, no score emitted
report-only + threshold failure      → status "fail", would_block true, CI exit 0
report-only + broken platform        → status "tooling-error", CI exit 3 (NOT swallowed)
type_check_command unset             → adapter runs off export-cicd-stats, no internals read
survivor fixed by simplifying code   → gate passes, no evasion flagged
docs-only change                     → status "skipped", not "pass"
Windows runner without WSL           → platform "unsupported", exit 3, NOT "skipped"
function count over cap              → sampled: true, same targets on rerun
wall-clock limit exceeded            → truncated: true, exit 3, NOT a pass
changed one line of a 40-line func   → whole function targeted; scope reported as
                                       functions_tested, not lines
suspicious outcome present           → excluded from scoreable, reported separately
```

**Symbol-to-target translation** needs its own test matrix. The diff→AST mapper produces
Python symbols (`billing.discount.calculate_discount`, `Foo.process`), and mutmut's internal
keys are not simply qualified names — there is mangling for methods and normalisation around
`src/` layouts and `__init__`. Assert `changed symbol → generated target → exactly the intended
mutant set` for:

```text
top-level function
class method
async function
__init__ method
nested / inner function
module under a src/ layout
```

**Do not let the wrapper invent mutmut's naming convention.** Reconcile against the mutant
metadata mutmut actually generates. An invented convention that happens to work for top-level
functions and silently misses every method is a gate that reports green on untested code.

The platform case, the wall-clock backstop, the `no tests` accounting and the name translation
are the ones most likely to be built wrong.

---

## 10. Acceptance criteria (Tier 3)

* [ ] scope is changed **functions**, derived via a diff→AST mapper, translated into mutmut
      target patterns — not whole-file runs with post-filtering
* [ ] symbol→target translation reconciled against generated mutant metadata, not invented,
      and self-tested across methods, async, `__init__` and `src/` layouts
* [ ] function cap with deterministic sampling via hash-and-sort per §3, not a seeded PRNG
* [ ] wall-clock backstop on the CI job; exceeding it reports `truncated` and exits 3
* [ ] exact-mutant-ID capping NOT built unless the function cap proved inadequate; if built,
      compatibility self-tests fail loudly on a mutmut version bump
* [ ] `mutate_only_covered_lines = true`; mutmut's native test selection used, no custom
      coverage→test mapping built
* [ ] outcome normalisation implemented and self-tested against the pinned version, with
      `no tests` in the denominator and incomplete states exiting 3
* [ ] outcome source documented: `export-cicd-stats` where sufficient, internals read only
      when `type_check_command` is configured, with compatibility self-tests if so
* [ ] timeout share flagged above 20% of `scoreable`; any `no_tests > 0` flagged
* [ ] `status` and `enforcement` reported independently; report-only exits 0 on threshold
      failure but 3 on any tooling failure
* [ ] platform capability reported; unsupported runner exits 3 and never reports `skipped`
* [ ] dedicated CI job; absent from `quality-fast`, pre-commit and the inner loop
* [ ] default floor 60%, configurable
* [ ] minimum `scoreable` of 20 enforced
* [ ] survivors emitted with path, symbol, line and mutation description
* [ ] full `outcomes` breakdown reported alongside the ratio
* [ ] all `# pragma: no mutate` forms — single, block, start/end — in the §9 added-line scan
* [ ] whole `[tool.mutmut]` semantic subtree protected via §11.2 hashing — not a key denylist
* [ ] no mutmut-specific ignore file exists; no allowlist integration in v1
* [ ] clean skip with `skipped` status when no eligible production functions changed
* [ ] self-tests from §9 above passing
* [ ] four weeks report-only completed before the gate blocks

---

## 11. Why this is the right thing to add next in Tier 3

It is the only remaining gate that creates an **independent adversary** for the tests. Every
other check in the system reads the code as written. Mutation testing changes the code and
demands the tests object.

That matters more for AI-generated Python than for human code, because a weak
coverage-shaped test is one of the cheapest ways for an agent to satisfy a task while leaving
false confidence behind — and unlike a missing test, it is invisible in every metric the rest
of this specification produces.

It is also, correctly, the last thing to build. It is the most expensive gate, the most
likely to produce unhelpful pressure on test design, and the one that benefits most from
being tuned against a repository already running everything else.
