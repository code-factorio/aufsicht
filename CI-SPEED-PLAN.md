# aufsicht CI speed plan

Status: proposal, not yet approved. All numbers below were measured or
verified in this repository on 2026-08-24. Estimates are marked as such.

## 1. Goal

Cut the CI wall clock without loss of function:

- Blacksmith: ~7 min today.
- GitHub Actions: 12-15 min today.

Keep every gate, every self-test, the read-only rule, and the spec
obligations (v5.1, distribution v1). The plan must be easy to implement
by LLM coding agents. Every step below names the files to change and the
process class of the change (normal PR or guardrail PR on a protected
path).

## 2. Measurements

Method: the full CI pipeline ran locally with a cold cache, in the same
steps and the same environment layout as `.github/workflows/aufsicht.yml`.
The local machine is slower than the CI runners; the GHA time (12-15 min)
matched the local total (~12.7 min), so the local proportions transfer.

| Phase (local, cold unless noted)              | Wall clock |
| --------------------------------------------- | ---------: |
| venv + `pip install . uv pytest`              |     5.8 s  |
| `aufsicht full` cold                          |   414.3 s  |
| — of which: pytest gate                       |   397.7 s  |
| — of which: all 12 other gates + env build    |    16.6 s  |
| `aufsicht full` warm (same cache root)        |   369.5 s  |
| Self-test step (`pytest tests -q`), cold dir  |   343.8 s  |
| Self-test step warm, `-n 4` xdist (verified)  |   131.6 s  |

Further measured facts:

- The suite holds 250 tests; 225 run, 25 skip. About 100 tests start
  the real CLI in a scratch repository. Each run starts ~18-20 analyzer
  subprocesses (13 gates; ratcheted tools run twice, HEAD and BASE).
- Each full-mode scratch run does one pip-audit network round trip:
  ~91 per suite run. No result cache exists (`pip_audit.py:299-335`).
- One analyzer environment is 434 MB. The job builds it up to three
  times, because three different cache roots are used (see §3.2).
- The repository is small: 15 commits, 1.41 MiB of git objects. A full
  `fetch-depth: 0` checkout costs ~1-3 s. It is not a problem.

## 3. Root causes, ranked

### 3.1 The test suite runs twice per CI run (the dominant cost)

`aufsicht full` contains a `pytest` gate. In full mode it collects
`testpaths = tests` (`.quality/pytest.ini`), so the gate runs the whole
self-test suite inside the pinned project env, with coverage
(`pipeline.py:33-37`, `pytest_adapter.py:386-408`). The workflow then
runs the same suite a second time as the step "Runner self-test suite"
(`aufsicht.yml:44-53`). Measured: 397.7 s + 343.8 s of a ~764 s total.
The two runs are not identical (pinned pytest + working tree vs ambient
interpreter + installed wheel), so both have value — but they do not
need to run one after the other.

### 3.2 Nothing is cached, and the job splits its cache over three roots

- The `quality-full` step sets no `AUFSICHT_CACHE_DIR`, so it uses the
  default `~/.cache/aufsicht` (`config.py:173-186`).
- `tests/conftest.py:26,61` forces every scratch CLI child to
  `AUFSICHT_TEST_CACHE` (default `/tmp/aufsicht-test-cache`).
- The self-test step exports `AUFSICHT_CACHE_DIR` =
  `${{ runner.temp }}/aufsicht-cache` (`aufsicht.yml:51-53`), which only
  the in-process test calls see.

No `actions/cache` exists anywhere. So every run builds the 434 MB
analyzer env from the network, up to three times. On top of that, the
scratch toolchain lock differs from the repository lock by three comment
lines, so even one shared root produces two distinct env keys
(`toolchain.py:296`, key = sha256 of lock bytes; byte diff verified).
v5.1 §4.4 does not merely allow lockfile-hash env caching — its
acceptance list requires it. CI never does it.

### 3.3 The suite is sequential, but it is parallel by construction

Every test works in its own `tmp_path` scratch git repo with a hermetic
git environment. Shared state is content-keyed on disk and guarded by an
O_EXCL lock (`toolchain.py:206-266`). No ports, no servers. Verified by
experiment: `pytest tests -q -n 4` passed with identical counts
(202 passed, 25 skipped) in 131.6 s vs 342.2 s sequential — 2.60x.

### 3.4 Small items

- ~91 pip-audit network round trips per suite run (see §2). This is a
  large part of the GHA penalty (GHA network latency is higher than
  Blacksmith's).
- Three identical changed-file `ruff check` invocations per run
  (ruff, ruff-s, complexity gates; `ruff.py:209-292`).
- No `concurrency` group and no `timeout-minutes`: superseded runs waste
  whole pipelines.
- A restored analyzer env is trusted by marker and shebang checks only;
  installed versions are never compared with the lock
  (`toolchain.py:176-203`). A bad cache restore can run wrong versions.
- `GateResult` has no duration field, so wins and regressions are not
  visible in the uploaded report.

## 4. The plan

Four milestones. Each is one or two PRs. Order matters only where noted.
Effort sizes assume an LLM coding agent with this document and the file
references.

### Milestone 1 — One workflow PR (protected path, biggest cut)

One guardrail PR against `.github/workflows/aufsicht.yml`. Needs its own
approved review (v5.1 §11.2/§11.3, deployment model B). Effort: small.

1. Job-level env for both jobs:
   `AUFSICHT_CACHE_DIR` and `AUFSICHT_TEST_CACHE` =
   `${{ runner.temp }}/aufsicht-cache` (one literal path, stable across
   runs — venv shebangs in a restored env must land on the same path).
   `conftest.py` respects both vars (`setdefault` at :27, `run_cli`
   override at :61), so the gate, the in-process test calls, and the
   scratch CLI children then share one root.
2. Add an `actions/cache` step before the installs:
   - `path`: the cache dir from (1).
   - `key`: `aufsicht-v1-${{ hashFiles('.quality/toolchain.lock', 'tests/fixtures/scratch.py', 'pyproject.toml') }}`
   - `restore-keys`: `aufsicht-v1-` (keeps base worktrees and ratchet
     counts warm across lock bumps; env dirs are content-keyed, so stale
     subdirectories are harmless).
   - Blacksmith accelerates `actions/cache` with no change (verified in
     their docs; ~2-3x faster restore than the GHA backend).
3. Move the self-test suite into its own job `selftests` that runs in
   parallel with `quality-full` (checkout, setup-python, install
   `. uv pytest pytest-xdist`, then `python -m pytest tests -q -n 4`
   with the same job-level cache env). Add `selftests` to the `publish`
   job's `needs`, so a self-test failure still blocks publishing
   (today it blocks it transitively through the quality-full job).
   v5.1 §18 requires self-tests in CI — it does not require them to run
   sequentially after the gate.
4. Add `concurrency` (`group: ${{ github.workflow }}-${{ github.ref }}`,
   `cancel-in-progress: ${{ github.event_name == 'pull_request' }}`) and
   `timeout-minutes: 30` to both jobs. The expression keeps
   cancel-away-from a main push that is about to publish.
5. Optional: `cache: pip` + `cache-dependency-path: pyproject.toml` on
   `setup-python` (saves ~15-40 s of wheel downloads).
6. Keep `fetch-depth: 0` (measured 1-3 s; the gate exits 3 without it).

Expected (estimate): Blacksmith ~7 -> ~4 min; GHA 12-15 -> ~6-7 min,
after the first run warms the cache. The first run after a lock bump
stays cold (~+1-3 min), amortized over all later runs.

### Milestone 2 — Test infrastructure (normal PR, not protected)

One normal PR in `tests/`. Effort: small.

1. `tests/conftest.py`: add a session-scoped autouse fixture that calls
   `aufsicht.toolchain.analyzer_env` on the scratch lock (and the
   default project env) before the first test. With xdist, each worker
   triggers it; the existing O_EXCL lock serializes the build, the
   others poll. This removes the multi-minute env build from inside the
   first test and stops worker build races.
2. `tests/fixtures/scratch.py:25-41`: add the three header comment lines
   so `SCRATCH_TOOLCHAIN` is byte-identical to `.quality/toolchain.lock`.
   Verified safe: `test_audit_fixes.py:85-91` and the other lock
   assertions only do substring/prefix checks. Effect: one analyzer env
   everywhere instead of two; ~430 MB less cache; one less cold build.

Expected (estimate): ~30-60 s on cold runs, smaller cache payload
(faster save/restore), no first-test timeout risk under xdist.

### Milestone 3 — Make the gate's own pytest run fast (two PRs)

The pytest gate runs the suite inside the pinned project env, which
contains only `pytest` and `pytest-cov` from `toolchain.lock`
(`toolchain.py:334-345`). xdist there needs a pin and a flag; both live
in protected files. Split the work so the runner PR is inert until the
guardrail PR lands:

- PR R (normal, `src/aufsicht`, not protected):
  a. `project_env_pins` accepts an optional `pytest-xdist` entry in the
     lock and installs it into the project env when present.
  b. The pytest adapter passes the `[tests] runner_args`
     (`config.py:132` already defines this hook) to the pinned pytest.
     Default stays sequential when no pin and no args exist.
  c. Add a `duration` field to `GateResult` (via `extra`, no collision
     with existing keys — verified) filled by the pipeline loop. The
     report artifact then shows per-gate cost for every future change.
     Note: distribution §10 classes a report schema change as MAJOR;
     bump the version in all five places with this PR.
  d. Harden cache hits: on a marker-valid env, read the installed
     versions from `dist-info` and compare them with the lock pins; on
     mismatch, rebuild (closes the wrong-version-restore gap,
     `toolchain.py:176-203`). No subprocess `--version` calls needed.
- PR G (guardrail, `.quality/**`, own approved PR):
  add `pytest-xdist = "<exact version>"` to `toolchain.lock` and
  `runner_args = ["-n", "auto"]` to `[tests]` in `.quality/config.toml`.

Expected (estimate): gate suite ÷ ~2.6 (measured xdist factor).
Blacksmith wall after M1+M2+M3: ~2-2.5 min; GHA: ~4-5 min.

### Milestone 4 — Network and consumer multipliers (optional, later)

1. pip-audit result cache (medium, runner PR + config key in a guardrail
   PR): cache parsed results under the aufsicht cache root, keyed on the
   freeze hash + pip-audit version + ignore-flag set, with a short TTL
   (e.g. 24 h) recorded in `[pip_audit]`. Fail closed: an unreadable or
   stale entry is a miss or exit 3, never a silent pass. The specs do
   not set a freshness rule for pip-audit (v5.1 §10.1 only notes alias
   drift), so document the TTL as a deliberate decision. Saves most of
   the ~91 network round trips per suite run — the main GHA penalty.
2. Consumer template (normal PR + version bump + publish):
   push the Milestone 1 pattern into `templates/workflows/aufsicht.yml`
   — cache step, job-level `AUFSICHT_CACHE_DIR`, concurrency, timeout.
   Every consumer repo then stops building the 434 MB env cold on every
   run. This is the multiplier for "aufsicht must be fast on machines
   slower than this repo's runners".
3. Parallel gate execution inside `run_pipeline` (medium, runner PR):
   bounded thread pool after the integrity gate (integrity must stay
   first, v5.1 §11.3), report assembled in `GATE_ORDER`, exit-3
   precedence preserved, a lock around `ratchet.base_worktree`, and the
   two shared-report writes synchronized. For this repo it saves only
   ~10 s (static gates are 16.6 s total), but for consumer repos with
   large codebases (two repo-wide pyright runs, semgrep) it is the
   difference between sum and max. Do it after (1c) makes per-gate
   durations visible, and only with the exit-code tests from
   `test_spine.py` kept green.
4. Deduplicate the three identical changed-file ruff invocations
   (small): one memoized `ruff check` per (cwd, file list); the ruff,
   ruff-s and complexity gates filter one finding set.
5. Deterministic commit dates in `tests/conftest.py` `GIT_ENV` plus the
   persisted ratchet cache: BASE analyzer runs then hit the cache across
   CI runs (keys already include base SHA + lock + config hashes). Only
   tests that assert on real dates (integrity expiry tests) keep them.

## 5. Directions considered and rejected (with data)

- Port aufsicht (or a library it uses) to Rust or another language.
  The runner's own compute is not the cost: all 12 non-pytest gates plus
  the env build are 16.6 s of a 414 s run (4%). The cost is the count of
  analyzer subprocesses, network calls, and env installs. A rewrite
  removes none of these. It also works against v5.1 §1 (the system must
  stay small enough for a human to read) and distribution §11 (no
  analyzer reimplementation). The one Python-side overhead — ~0.5 s CLI
  startup × ~100 scratch runs ≈ 50 s per suite run — is beaten by xdist
  at a fraction of the risk.
- Reduce `fetch-depth`. Measured 1-3 s on this repo; the gate exits 3
  on a shallow clone by design (v5.1 §4.6). Only very large consumer
  histories could gain, and a partial clone deviates from the spec text.
- Delete the self-test step (run the suite only inside the gate), or
  disable the pytest gate for this repo (run the suite only in the
  step). Both cut more than Milestone 1 but each gives up half the
  coverage intent: the gate run proves the pinned env + coverage
  telemetry; the step run proves the installed wheel under the ambient
  interpreter. The parallel-job split keeps both. If job minutes ever
  matter more than coverage, dropping the gate's pytest for this repo
  (a `[gates]` config change, guardrail PR) is the documented fallback.
- Skip pip-audit in scratch repositories to save the network calls.
  Rejected in favor of the result cache: disabling narrows what the
  self-tests exercise; the cache keeps every run real and only removes
  duplicate network work.

## 6. Expected end state (estimates)

| Stage                                | Blacksmith | GitHub Actions |
| ------------------------------------ | ---------: | -------------: |
| Today                                |      ~7 min |       12-15 min |
| After Milestone 1                    |     ~4 min |        ~6-7 min |
| After Milestones 1-3                 |  ~2-2.5 min |        ~4-5 min |
| First run after a cache miss         |  +1-3 min  |        +1-3 min |

Milestone 4.1 removes most of the remaining GHA penalty;
4.2 multiplies the win across every consumer repository.

## 7. Verification

- Milestone 1: the report artifact stays identical in shape; CI timings
  drop; the cache pane shows a hit after run two; a pushed scratch
  change to `toolchain.lock` misses the exact key and falls back to the
  prefix restore-key.
- Milestone 3c: `aufsicht-report.json` gains one `duration` per gate;
  existing report tests stay green (no exhaustive key-set assertions —
  verified).
- After each milestone: same pass counts (202 passed, 25 skipped) in
  both suite executions; exit codes 0/1/2/3 unchanged
  (`test_spine.py`, `test_selftests.py` cover them).
- Regression tripwire: per-gate durations from (3c) compared across
  runs in the uploaded artifacts.

## 8. Open decisions for the maintainer

1. Cancel-in-progress scope (Milestone 1.4): the expression above
   cancels superseded PR runs only. Confirm that main pushes should
   never be cancelled mid-publish.
2. pip-audit TTL (Milestone 4.1): the specs are silent on freshness.
   24 h is the proposal; a shorter window trades speed for detection
   latency of new advisories.
3. Report schema bump (Milestone 3c): confirm the distribution §10
   classification (MAJOR) before the version bump in the five places.
4. Sequencing: Milestones 2 and 3-PR-R can land in any order relative to
   Milestone 1; Milestone 3-PR-G must follow 3-PR-R.
