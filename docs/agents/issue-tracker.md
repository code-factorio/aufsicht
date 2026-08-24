# Issue tracker: Rohrpost

Issues and specs for this repo live in **[Rohrpost](https://github.com/code-factorio/rohrpost)**, a git-native tracker. Tickets are events in `.rohrpost/log.jsonl`, committed with the code. There is no external service to reach and no daemon to run, so the tracker branches and merges like the code does.

## Invoking `rp`

`rp` walks up from the current directory to find `.rohrpost/`, so it works from anywhere in the repo. Every command accepts `--json`; always pass `--json` for agent-facing operations. Ticket ids are bare (`a1b2c3`) or rendered with the project's display prefix (`RP-a1b2c3`); `rp` accepts either. Prefer the bare id in scripts.

## Conventions

- Create a ticket with `rp new "<title>" --body "<markdown>" --json`.
- Read a ticket with `rp show <id> --include body,deps,notes --json`.
- List tickets with `rp list --status open --json`; filter with `--label`, `--type`, `--parent`, or `--match` as needed.
- Find ready work with `rp ready --json`.
- Comment with `rp comment <id> "<note>"`.
- Claim work with `rp claim <id>`.
- Close work with `rp close <id> --reason "<why>"`.

Statuses are `open`, `in_progress`, `review`, `done`, `waiting`, and `dropped`. `ready` is derived: a ticket is ready when it is open, is not an epic, and all blockers are done.

## Pull requests as a request surface

**PRs as a request surface: no.** Set this to `yes` only if external PRs should be copied into the Rohrpost triage queue.
