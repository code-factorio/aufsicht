#!/usr/bin/env bash
# aufsicht bootstrap — the single canonical installer entry point.
#
# This file is copied byte-identically to skill/scripts/bootstrap.sh by a
# build step, and CI asserts equality (distribution spec §9). Keep it
# thin: verify prerequisites, install the pinned runner as a tool, hand
# off to `aufsicht init`. If this script starts writing .quality/ itself
# it has become a second implementation of Layer 2 on an independent
# update cadence, and the layering invariant is gone.

set -euo pipefail

# The pinned runner version appears in three places — this bootstrap,
# .quality/toolchain.lock in the target repo, and the JSON report. If
# this copy ever drifts, it shows up as a diff rather than as two
# repositories mysteriously behaving differently.
AUFSICHT_VERSION="0.2.5"

say() { printf 'bootstrap: %s\n' "$*" >&2; }
die() { printf 'bootstrap: ERROR: %s\n' "$*" >&2; exit 1; }

command -v git >/dev/null 2>&1 \
  || die "git not found — aufsicht guards existing git repositories"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || die "not inside a git repository (run from the repository root)"

if command -v uv >/dev/null 2>&1; then
  say "installing aufsicht ${AUFSICHT_VERSION} as an isolated uv tool"
  uv tool install --force "aufsicht==${AUFSICHT_VERSION}" >&2
elif command -v pipx >/dev/null 2>&1; then
  say "installing aufsicht ${AUFSICHT_VERSION} with pipx"
  pipx install --force "aufsicht==${AUFSICHT_VERSION}" >&2
else
  die "neither uv nor pipx found. Install uv (recommended): https://docs.astral.sh/uv/"
fi

command -v aufsicht >/dev/null 2>&1 \
  || die "aufsicht installed but not on PATH — restart your shell or check the tool bin directory"

say "handing off to 'aufsicht init' (prints a plan first; add --write to apply it on a branch)"
exec aufsicht init "$@"
