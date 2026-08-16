"""Xenon adapter: aggregate complexity count ratchet (v5.1 §6.2, §8 last row).

One gate, full mode only:

  gate "xenon"  the ratcheted integer is the COUNT of modules whose
                average block complexity ranks above --max-modules,
                compared BASE → HEAD (v5.1 §8: "modules over Xenon
                --max-modules > base"). Single metric, single integer —
                Xenon produces no classified finding set to group.

Complexity ownership (v5.1 §6.2): Ruff C901 gates per-function,
diff-scoped; Xenon gates the aggregate; pyscn complexity is telemetry.
--max-absolute is deliberately loose so it never fights C901 — a block
over it is C901's subject first, so this gate reads only the module
lines, not xenon's exit code or its block/average infractions.

Output shape — MEASURED against the pinned xenon 0.9.3
(/tmp probe, modules of known complexity):

  * No --json exists (only -u/--url, which POSTs remotely). Text parse.
  * Everything is logged to STDERR in Python logging format,
    e.g. ``ERROR:xenon:module 'src/pkg/mod.py' has a rank of B``;
    stdout is empty; exit 0 clean, 1 any infraction, 3 only for
    -u HTTP errors.
  * ``module '<path>' has a rank of <L>`` is emitted ONLY for modules
    strictly above the --max-modules letter, so counting lines IS the
    count — xenon has already made the over/under decision.
  * Rank scale, measured at the boundaries with modules whose function
    complexities average exactly 5, 10, 20, 30, 40 and +0.5 past each:
    A ≤ 5 < B ≤ 10 < C ≤ 20 < D ≤ 30 < E ≤ 40 < F — radon's BLOCK
    cc_rank applied to the module average, NOT a separate module scale.
    Encoded below for letter validation and report messages only.
  * Unparseable modules become ``WARNING:xenon:cannot parse <path>:``
    and are silently dropped from the count — fail closed on them,
    an understated count is indistinguishable from a pass.
  * ``--paths-in-front`` inverts the message shape; this adapter never
    passes it.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from .. import ratchet as ratchet_mod
from ..config import CONFIG_PATH
from ..errors import ToolingError
from ..model import (
    Finding,
    GateResult,
    GATE_EXEMPT,
    GATE_FAIL,
    GATE_PASS,
    GATE_SKIPPED,
    MECH_COUNT_RATCHET,
)
from ..pipeline import MODE_FULL, GateContext, gate

# The one synthetic ratchet bucket: xenon yields a single count, not
# per-rule findings, so every over-threshold module lands here and the
# spine's count_by_rule/compare do the integer comparison (v5.1 §4.3).
MODULE_OVER = "xenon/module-over"

# MEASURED (xenon 0.9.3, boundary probe): the letter a module gets for
# its average block complexity, upper bound inclusive per letter.
# Used to validate configured letters and to word findings — never to
# re-derive xenon's own over/under decision.
RANK_MAX: dict[str, int | None] = {
    "A": 5,
    "B": 10,
    "C": 20,
    "D": 30,
    "E": 40,
    "F": None,
}

# Both against the measured stderr shapes; the logging prefix is
# optional so a format tweak upstream does not silently zero the count.
MODULE_OVER_RE = re.compile(
    r"module '(?P<module>.*?)' has a rank of (?P<rank>[A-F])\s*$"
)
CANNOT_PARSE_RE = re.compile(r"cannot parse (?P<module>.*?): ")

DEFAULT_LETTERS = ("A", "A", "C")


def rank_range(letter: str) -> str:
    """Human wording for a measured rank, e.g. ``B → (5, 10]``."""
    upper = RANK_MAX[letter]
    if letter == "A":
        return "[0, 5]"
    lower = RANK_MAX[chr(ord(letter) - 1)]
    return f"({lower}, {upper}]" if upper is not None else f"({lower}, ∞)"


def _checked_letters(letters: tuple[str, str, str]) -> tuple[str, str, str]:
    for name, letter in zip(
        ("xenon_max_average", "xenon_max_modules", "xenon_max_absolute"), letters
    ):
        if letter.upper() not in RANK_MAX:
            raise ToolingError(
                f"{name} must be one of A..F, got {letter!r}",
                remedy="Fix [complexity] in .quality/config.toml — xenon "
                "compares rank letters, an unknown letter gates nothing.",
            )
    return (letters[0].upper(), letters[1].upper(), letters[2].upper())


def base_xenon_letters(repo: Path, sha: str) -> tuple[str, str, str]:
    """BASE's [complexity] xenon letters at *sha* (v5.1 §4.3: BASE source
    is analysed under BASE's configuration)."""
    blob = ratchet_mod.read_file_at(repo, sha, CONFIG_PATH)
    if blob is None:
        return DEFAULT_LETTERS
    try:
        data = tomllib.loads(blob.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return DEFAULT_LETTERS
    table = data.get("complexity", {})
    if not isinstance(table, dict):
        return DEFAULT_LETTERS
    return (
        str(table.get("xenon_max_average", DEFAULT_LETTERS[0])),
        str(table.get("xenon_max_modules", DEFAULT_LETTERS[1])),
        str(table.get("xenon_max_absolute", DEFAULT_LETTERS[2])),
    )


def source_root(cwd: Path) -> str:
    """The source tree argument: src/ when the repo has one, else ."""
    return "src" if (cwd / "src").is_dir() else "."


def run_xenon(ctx: GateContext, cwd: Path, letters: tuple[str, str, str]) -> str:
    """Run the pinned xenon on *cwd*'s source tree; return its stderr
    (measured: all diagnostics are logged there, stdout stays empty)."""
    max_average, max_modules, max_absolute = _checked_letters(letters)
    proc = ctx.run(
        "xenon",
        "--max-average",
        max_average,
        "--max-modules",
        max_modules,
        "--max-absolute",
        max_absolute,
        source_root(cwd),
        cwd=cwd,
    )
    # Measured exit codes: 0 clean, 1 any infraction (module, block or
    # average — only the module lines gate here), 3 for -u HTTP errors.
    if proc.returncode not in (0, 1):
        raise ToolingError(
            f"xenon failed (exit {proc.returncode}): {(proc.stderr or proc.stdout)[:500]}",
            remedy="Check the pinned xenon version in .quality/toolchain.lock "
            "and the [complexity] letters in .quality/config.toml.",
        )
    return proc.stderr


def parse_modules_over(stderr_text: str) -> list[tuple[str, str]]:
    """(module path, rank) for every module over --max-modules.

    Fail closed on unparseable modules: xenon logs a warning and drops
    them from the count (measured), so counting on would understate the
    aggregate — indistinguishable from a pass.
    """
    over: list[tuple[str, str]] = []
    unparseable: list[str] = []
    for line in (stderr_text or "").splitlines():
        m = MODULE_OVER_RE.search(line)
        if m:
            over.append((m.group("module"), m.group("rank")))
            continue
        w = CANNOT_PARSE_RE.search(line)
        if w:
            unparseable.append(w.group("module"))
    if unparseable:
        raise ToolingError(
            f"xenon could not parse {len(unparseable)} module(s): "
            f"{', '.join(sorted(set(unparseable))[:5])}",
            remedy="Fix the syntax error — an uncounted module makes the "
            "complexity ratchet's count unsound.",
        )
    return over


def _counts(
    ctx: GateContext, cwd: Path, letters: tuple[str, str, str]
) -> dict[str, int]:
    over = parse_modules_over(run_xenon(ctx, cwd, letters))
    return ratchet_mod.count_by_rule([MODULE_OVER] * len(over))


@gate("xenon")
def xenon_gate(ctx: GateContext) -> GateResult:
    # Full mode only (v5.1 §5: aggregate ratchets are CI-sized); the
    # pipeline's GATE_ORDER already enforces it — this is the contract.
    if ctx.mode != MODE_FULL:
        return GateResult(
            name="xenon",
            status=GATE_SKIPPED,
            mechanism=MECH_COUNT_RATCHET,
            detail="full mode only (v5.1 §8 ratchet table)",
        )

    letters = _checked_letters(
        (
            ctx.config.xenon_max_average,
            ctx.config.xenon_max_modules,
            ctx.config.xenon_max_absolute,
        )
    )

    if ctx.is_ratchet_exempt("xenon"):
        # Visible exemption, never a silent pass (v5.1 §4.5, §15).
        return GateResult(
            name="xenon",
            status=GATE_EXEMPT,
            mechanism=MECH_COUNT_RATCHET,
            detail="ratchet exempt this run; modules-over count not compared",
            extra={
                "ratchet": "exempt",
                "ratchet_reason": ctx.report.exemption_reasons.get("xenon", "exempt"),
            },
        )

    base_letters = base_xenon_letters(ctx.repo, ctx.base.sha)
    key = ratchet_mod.cache_key(
        base_sha=ctx.base.sha,
        tool="xenon",
        lock_hash=ctx.lock.raw_hash,
        config_hash="config:" + "/".join(base_letters),
    )
    base_wt = ratchet_mod.base_worktree(ctx.repo, ctx.base.sha, ctx.cache)
    base_counts = ratchet_mod.cached_base_counts(
        key, ctx.cache, lambda: _counts(ctx, base_wt, base_letters)
    )

    head_over = parse_modules_over(run_xenon(ctx, ctx.repo, letters))
    head_counts = ratchet_mod.count_by_rule([MODULE_OVER] * len(head_over))
    outcome = ratchet_mod.compare(base_counts, head_counts)
    base_n, head_n = outcome.totals

    extra = {"base": base_n, "head": head_n, "ratchet": outcome.to_dict()}
    detail = f"{head_n} module(s) over --max-modules {letters[1]} (base {base_n})"

    if not outcome.passed:
        findings = [
            Finding(
                path=module,
                line=0,
                rule=MODULE_OVER,
                message=(
                    f"module average complexity rank {rank} is over "
                    f"--max-modules {letters[1]} (rank {rank} = average "
                    f"block complexity {rank_range(rank)}, measured scale) — "
                    "aggregate drift; nothing tripped C901 individually "
                    "(v5.1 §6.2)"
                ),
            )
            for module, rank in head_over
        ]
        return GateResult(
            name="xenon",
            status=GATE_FAIL,
            mechanism=MECH_COUNT_RATCHET,
            detail=detail + " — regressed against the merge base",
            findings=findings,
            extra=extra,
        )
    return GateResult(
        name="xenon",
        status=GATE_PASS,
        mechanism=MECH_COUNT_RATCHET,
        detail=detail,
        extra=extra,
    )
