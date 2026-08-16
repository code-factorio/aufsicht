"""Global probe results — answered once, baked into the runner
(distribution spec §6).

Global probes are re-run at install time as assertions against the
pinned analyzer, not as decisions. If a probe disagrees with what the
runner believes, that is exit 3 and a version-bump bug, loudly, not a
silent branch.

Answers below were measured by running the tools:

* Ruff C901 ``end_location`` spans the ``def`` line only, not the
  function body (verified against 0.14.5 and re-asserted against the
  pinned version at install). Per v5.1 §4.2 this settles Tier 1
  policy: **changed files must satisfy C901** — changed-file scope,
  accepting the stricter treatment of legacy functions. We do not claim
  changed-function semantics while implementing changed-file semantics.
* mutmut's outcome vocabulary is the state list recorded here
  (verified against 3.7.0 source; ``export-cicd-stats`` omits
  ``caught_by_type_check`` and ``not_checked`` — addendum §4.1's
  conditional contract).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .errors import ToolingError

# v5.1 §4.2: "file" | "function". The measured Ruff behaviour gives the
# def line only, so the Tier 1 policy is changed-file scope.
C901_SCOPE = "file"

# Ruff versions against which the C901 span answer was verified.
C901_PROBE_VERIFIED = ("0.14.5",)

# Fixture: a function whose body clearly extends beyond the def line,
# with cyclomatic complexity above 10.
C901_PROBE_SOURCE = "def complex_function(x):\n" + "".join(
    f"    if x == {i}:\n        return {i}\n" for i in range(1, 13)
) + "    return 0\n"

# mutmut outcome states (addendum §4), verified against mutmut 3.7.0.
MUTMUT_PROBE_VERIFIED = ("3.7.0",)
MUTMUT_OUTCOME_STATES = (
    "killed", "timeout", "survived", "no_tests", "suspicious",
    "skipped", "caught_by_type_check", "segfault", "not_checked",
    "interrupted",
)


def assert_c901_span(ruff_exe: str | Path) -> None:
    """Re-run the C901 global probe against *ruff_exe*.

    The assertion: for a function whose body spans many lines, the C901
    diagnostic's end row equals its start row (the def line). If a
    future Ruff starts spanning the body, this raises — the runner's
    C901_SCOPE= "file" policy would be silently wrong.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "complex.py"
        src.write_text(C901_PROBE_SOURCE, encoding="utf-8")
        proc = subprocess.run(
            [str(ruff_exe), "check", "--isolated", "--select", "C901",
             "--output-format", "json", str(src)],
            capture_output=True, text=True, timeout=120,
        )
        try:
            findings = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise ToolingError(
                f"C901 global probe: ruff produced unparseable output: {proc.stderr[:300]}"
            ) from exc
        c901 = [f for f in findings if f.get("code") == "C901"]
        if not c901:
            raise ToolingError(
                "C901 global probe: pinned ruff did not report C901 for a "
                "complexity-13 function; probe fixture or ruff behaviour changed"
            )
        diag = c901[0]
        start_row = diag["location"]["row"]
        end_row = diag["end_location"]["row"]
        n_source_lines = C901_PROBE_SOURCE.count("\n")
        if not (start_row == end_row == 1 and n_source_lines > 5):
            raise ToolingError(
                "C901 global probe disagreement: pinned ruff reports a span "
                f"({start_row}..{end_row}) that no longer covers only the def "
                f"line; aufsicht {C901_SCOPE}-scope policy is stale",
                remedy="This is a version-bump bug in aufsicht: re-run the "
                       "global probes and update C901_SCOPE (distribution "
                       "spec §6).",
            )


def assert_mutmut_vocabulary(mutmut_exe: str | Path) -> None:
    """Re-run the mutmut vocabulary probe against *mutmut_exe*.

    The pinned distribution must still expose the outcome states the
    adapter normalises; a rename would silently miscount.
    """
    proc = subprocess.run(
        [str(mutmut_exe), "--version"], capture_output=True, text=True,
        timeout=120, cwd="/",
    )
    if proc.returncode != 0:
        # mutmut --version outside a configured project exits non-zero on
        # some versions; the state names live in its Python package.
        probe = subprocess.run(
            ["python", "-c",
             "import mutmut.__main__ as m, inspect; src = inspect.getsource(m); "
             "import sys; sys.exit(0 if all(s in src for s in "
             f"{list(MUTMUT_OUTCOME_STATES)!r}) else 1)"],
            capture_output=True, text=True, timeout=120,
            env={"PATH": str(Path(mutmut_exe).parent) + ":/usr/bin:/bin",
                 "HOME": "/tmp"},
        )
        if probe.returncode != 0:
            raise ToolingError(
                "mutmut vocabulary probe disagreement: outcome states not "
                "found in the pinned mutmut",
                remedy="Version-bump bug: update MUTMUT_OUTCOME_STATES in "
                       "aufsicht for the new mutmut (addendum §4).",
            )
