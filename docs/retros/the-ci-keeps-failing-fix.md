# Retro: The CI wasn't failing — the release was waiting for you

**Date**: 2026-09-01
**Session**: 01a05a28-ed6b-7e34-97ad-fede7d6aff85
**Transcript**: /home/vfeenstr/.pi/agent/sessions/--home-vfeenstr-devel-lab-code-factorio-aufsicht--/2026-08-31T23-30-26-795Z_01a05a28-ed6b-7e34-97ad-fede7d6aff85.jsonl
**Duration / tokens / cost**: 522 s; 527,973 total tokens (44,368 input / 16,021 output / 467,584 cache-read / 12,518 reasoning); $0.00; 32 tool calls (27 bash, 4 read, 1 ask_user), 1 bash error
**Extraction**: /home/vfeenstr/devel/lab/code-factorio/aufsicht/docs/retros/the-ci-keeps-failing-fix.extract.json

## What happened

The user asked to fix the "failing" CI and push the fix, then to watch it until it was really fixed (e1dcb407, 750b4ee9). The agent pulled origin/main (4 commits ahead, ff-only, 82867bb9) and diagnosed two things: past red runs were the `integrity` tripwire correctly rejecting `.quality/toolchain.lock` bundled into feature PRs (#21–#23, all since landed), and the latest main run's `publish` job had sat in `waiting` for 132 h because the `pypi` GitHub environment has a required-reviewer rule for `vinzenz` (3f89a7e7, fc70fbf1, 3bfd6a0c). There was no code defect and nothing to push. The agent asked the user how to unblock the approval rather than self-approving with its own credentials (2cc90b6b → 9dfadda2), the user approved in the UI, the run went green, and the agent verified jobs and PyPI end-to-end (81b54400, da032d15, 0a9443db). A follow-up question about supporting Rust codebases (40849cea) needed one clarifying correction from the user (ff17996b) and produced a grounded estimate anchored in the vendored spec and per-gate adapters (77e8be5b).

## What worked

- **Refusing to self-approve the deployment** (2cc90b6b, b5993335): the agent noticed `gh` was authenticated as `vinzenz` — the required reviewer — and still surfaced the approval as an `ask_user` choice instead of approving via API. Matches AGENTS.md's "do not self-approve" spirit at the infra level, not just the gate level.
- **Verifying "really fixed" end-to-end** (1ea8e1d8, da032d15, 0a9443db): after the run went green it confirmed all three jobs and that `aufsicht 0.2.5` (wheel + sdist) was actually live on PyPI before declaring done.
- **Grounding the estimate in primary sources** (6705a279–77e8be5b): the Rust answer is a gate-by-gate mapping against the vendored spec's §6 tool table and the actual adapters, with "the spec is the first blocker" as the conclusion — not a guess.
- **The `domain.md` silent-proceed path worked as designed** (e1cb3f57, 33037bcf): the probe for the nonexistent `CONTEXT.md`/`docs/adr` errored once and the agent moved on without flagging it, exactly as `docs/agents/domain.md` prescribes. Not friction.

## Friction

- **`pypi` approval knowledge exists nowhere in the repo** (3f89a7e7, 3b1063fc, fc70fbf1, 3bfd6a0c, 4a565d70): "run stuck in `waiting`" had to be traced from run list → job view → API steps (empty) → workflow `environment:` → environments API → required-reviewer rule, ~6 calls across 3 turns. The workflow comment (`aufsicht.yml:151-155`) explains only why the environment claim is needed for OIDC, never that a human approval gate sits behind it; the distribution spec and the AGENTS.md Releases section say nothing either. Root cause: the reviewer rule lives in GitHub settings, outside the repo, so no in-repo document records it; every future release will re-derive it on the next "CI seems stuck" session.
- **Log tail instead of log search** (4b5b8337 → 9f3dc26a → 99ae03a1): `gh run view --log-failed | tail -80` returned 8,294 bytes consisting of the trailing JSON quality report, not the failing gate; an immediate `rg -i "fail|error|ratchet"` retry found it. Root cause: in this repo a failed `quality-full` log ends with the JSON report, so the tail is structurally the wrong place to look; no guidance anywhere says "grep the log" — `babysit-pr` §4 teaches the `--log-failed` command without a search hint, and that skill is user-invoked only (`disable-model-invocation: true`), so nothing else covers it.
- **Ambiguous scope question cost one user correction** (40849cea, 4df46c83, ff17996b, 6705a279): "support rust" was read as "make the runner less Python-coupled" and explored silently for two tool turns (e2f5b62b, f00a5d3a) before the user interjected "with rust I mean a rust codebase". The exploration direction was already correct and all four pre-correction calls were reused after it, so the waste was one turn, not work — but the user had to intervene because no visible sentence ever said "you mean aufsicht gains gates for Rust repos; aufsicht stays Python". Root cause: generic — the agent stated a method ("how tightly the runner is coupled to Python") but never its interpretation of the request itself before going quiet.

## Proposals

| # | Type | Change | Where | Evidence | Status |
|---|------|--------|-------|----------|--------|
| 1 | rule-update | Add a deployment-approval paragraph to the Releases section: after "…bump all five in the same commit, semver per distribution spec §10.", insert: "**Deployment approval**: the `publish` job runs in the `pypi` GitHub environment, which has a required-reviewer rule. Every green push to main sits in `waiting` with no started steps until the release is approved (Actions → run → review deployments). A run stuck in `waiting` is an approval gate, not a CI failure — do not hunt for a code defect." | `AGENTS.md`, section "Releases: every green push to main publishes" (protected path — apply via your review, not an agent edit) | 3f89a7e7, fc70fbf1, 3bfd6a0c | done — user applies the paragraph themselves (protected path; agent edit declined) |
| 2 | skill-update | In §4 "Clear red CI runs", after "Read the failed run logs with `gh run view <run-id> --log-failed` before touching anything.", insert: "Search the log (`… --log-failed \| rg -i 'fail\|error\|ratchet'`) instead of reading its tail — a failed aufsicht `quality-full` log ends with the JSON report, and the failing gate sits earlier. Add a fifth classification: **Blocked**: the run is in `waiting` with no started steps — a deployment-environment approval gate (see AGENTS.md, Releases), not a failure." | `.agents/skills/babysit-pr/SKILL.md`, section "4. Clear red CI runs" | 4b5b8337, 99ae03a1, fc70fbf1 | done — applied on top of the local edits in the working tree (uncommitted) |
| 3 | acknowledge | Do not change the repo for the "with rust I mean a rust codebase" correction: direction was already correct, all pre-correction calls were reused, cost was one turn. The durable habit — state your interpretation of an ambiguous request in your first visible sentence before exploring silently — is generic agent behavior, not repository knowledge. | — | 40849cea, 4df46c83, ff17996b | done (acknowledged — no change required) |

Ranked: #1 first — it is a deterministic infra fact every future release session will need, in the file read at session start; #2 is a one-line sharpening of an existing skill.

## Questions for the user

- Proposal 2 edits `babysit-pr/SKILL.md`, which currently has uncommitted local modifications. Apply on top of your edits, or hold until those are committed?
- `AGENTS.md` is a protected path per its own rules. Confirm you want to apply proposal 1 yourself (or approve the parent session doing it as a reviewed change).
