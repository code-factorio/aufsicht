---
name: babysit-mr
description: Drive a merge request towards merge - answer the review comments, clear a red pipeline, sync with the target branch, polish title and description, and keep the Jira ticket accurate.
disable-model-invocation: true
---

# Babysit a merge request

An MR is not a task, it is an artifact that lives for days: reviewers comment, the target branch moves underneath it, the pipeline goes red, and the Jira ticket quietly stops reflecting reality. Babysitting drives it towards one state:

**Every thread answered, pipeline green, branch current, title and description honest, Jira accurate.**

Each invocation drives the MR as far as it can *right now*, then stops and reports. It never waits for a pipeline and never sits watching - the user runs it again when there is something new to react to.

## 1. Resolve the target

From `!123`, a branch name, or "my MR", establish: the GitLab project, the MR iid, the source and target branch, and the Jira key. Derive the Jira key from the branch name, then the MR title, then the description.

State what you resolved before touching anything - a wrong target caught here costs nothing.

Done when: you hold project, iid, both branch names, and either a Jira key or an explicit "no ticket linked".

## 2. Assess, and report before acting

Take the snapshot first:

- Open discussion threads, split into those still awaiting a reply from your side and those already answered and waiting on their creator.
- Pipeline status of the latest commit, and which jobs failed.
- Commits behind the target branch.
- Jira state, against what the MR's current state implies.
- Title and description, against the project's template.

Hand the user this snapshot before changing anything. It is worth the invocation on its own, and it is the checkpoint where they redirect you.

Done when: the user has the snapshot and you know which of the five need work.

## 3. Answer the review comments

**Reply, and leave the thread open - its creator resolves it.** That is the team's convention, and it holds even when the fix is obvious and already pushed. Your reply says what changed and points at the commit; the decision that it is settled belongs to whoever raised it.

Sort every open thread into three piles:

- **Mechanical** - a named change with one obvious correct implementation: a rename, an extracted function, a missing null check, a typo, an absent test case. Fix it, reply pointing at the commit.
- **Substantive** - questions the design, disputes the approach, asks *why*, or leaves you guessing at intent. Draft the reply and surface it to the user before posting. These are the threads where a confident wrong answer costs the most.
- **Already satisfied** - a later commit handled it. Reply pointing at that commit.

Commit so the mapping is visible - one commit per thread, or one commit naming the threads it answers. The reviewer re-reads the diff far faster when the commits line up with their comments.

Mark agent-authored replies as such, in whatever form the team uses.

Done when: every open thread carries a reply, or a drafted reply awaiting the user, and you can name which is which.

## 4. Clear a red pipeline

Pull the failed jobs for the head commit and read their logs before touching anything. Sort each failure:

- **Real** - your code or your test broke it. Fix the cause. **Never weaken an assertion, skip a test, or loosen a lint rule to reach green** - that converts a visible failure into an invisible one.
- **Flake** - non-deterministic, unrelated to the diff, passes on rerun. Retry the job with `gitlab_pipeline_job_retry`. Say that you did, and say which job, so a genuine intermittent bug is not buried by a quiet retry.
- **Infrastructure** - runner, registry, network, credentials. Not yours to fix in the diff; report it with the evidence from the log.
- **Pre-existing** - the target branch is red too. Not yours. Name it and move on.

When the repo has a local test or lint command, run it after your changes and before pushing. A CI cycle costs minutes; a local run costs seconds.

Done when: every failed job is classified, real failures are fixed at the cause, and anything you are not fixing is reported with its reason.

## 5. Sync with the target branch

Only when the MR is behind.

Follow the project's convention - rebase or merge, read from the repo rather than assumed. **Confirm before rewriting history**, and push with `--force-with-lease`.

Two stop conditions:

- **Someone else has commits on the source branch.** Stop and ask. Rebasing a shared branch destroys their work.
- **Conflicts.** Hand off to the `resolving-merge-conflicts` skill rather than improvising.

Push once, after the comment fixes, the pipeline fixes, and the sync are all in.

Done when: the source branch carries the target's latest and is pushed, or the user declined, or conflicts were handed off.

## 6. Polish title and description

Read the project's template from `.gitlab/merge_request_templates/`. Derive the title convention from the last handful of merged MRs in the project rather than assuming one - that is evidence, where a guess is a guess.

The description says what changed and why, links the Jira issue, and names anything the reviewer needs to know before reading the diff.

Done when: the title matches the project's observed convention, the description follows the template, and the Jira link is present.

## 7. Update Jira

Call `jira_get_transitions` first - the valid ids depend on the issue's current state - then transition per the team's mapping of MR state to Jira state. Take that mapping from repo config; inferring it produces confident, wrong transitions, and "merged" does not mean "done" on a team with a verify state.

Add a comment covering what moved since the last one. Someone following the ticket without reading the MR should learn something from it.

Done when: the Jira state matches the mapping for the MR's current state, the MR is linked, and the comment describes what changed since the previous update.

## 8. Stop and report

Close every invocation with:

- What changed - commits pushed, threads answered, jobs retried, fields updated.
- What is blocked on the user - substantive replies drafted, a rebase awaiting confirmation, an infrastructure failure, a design question.
- What is blocked on a reviewer - threads you answered that only their creator can resolve.
- What the next invocation picks up - the pipeline you did not wait for, named by id.

Done when: the user knows the MR's current state, their own next action, and yours.

## Configuration this expects

Per repo or per team, and worth writing down once rather than rediscovering each run:

- GitLab project id, and how a branch name maps to a Jira key.
- Rebase or merge for keeping up with the target branch.
- MR state to Jira state mapping.
- How agent-authored comments are marked.
