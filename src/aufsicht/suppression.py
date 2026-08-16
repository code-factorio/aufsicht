"""Added-line suppression scan (v5.1 §3 table row 4; addendum §6).

`# type: ignore` on an added line is an inline suppression comment, and
there is exactly one sanctioned exit (v5.1 §10). The scan covers the
suppression pragmas the tools themselves honour — including mutmut's
`# pragma: no mutate` in all its forms (addendum §6) — on lines added
by this change only. Legacy suppressions elsewhere are not this gate's
subject; the ratchets own the counts.
"""

from __future__ import annotations

import re

from .model import Finding

# Suppression comment patterns. Matched anywhere in the added line,
# case-insensitive for the directive word.
SUPPRESSION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("type-ignore", re.compile(r"#\s*type:\s*ignore\b")),
    ("pyright-ignore", re.compile(r"#\s*pyright:\s*ignore\b")),
    ("noqa", re.compile(r"#\s*noqa\b")),
    ("pylint-disable", re.compile(r"#\s*pylint:\s*disable\s*=")),
    ("pragma-no-cover", re.compile(r"#\s*pragma:\s*no\s*cover\b")),
    ("pragma-no-mutate", re.compile(r"#\s*pragma:\s*no\s*mutate\b")),
    ("pragma-no-mutate-block", re.compile(r"#\s*pragma\b.*\bno\s*mutate\b")),
    ("coverage-ignore", re.compile(r"#\s*coverage:\s*ignore\b")),
    ("mypy-ignore", re.compile(r"#\s*mypy:\s*ignore\b")),
)


def scan_added_lines(
    added_lines: dict[str, list[int]], read_line
) -> list[Finding]:
    """Scan added lines via *read_line(path, n) -> str | None*.

    Kept as a callback so callers control file IO (paths are
    repo-relative POSIX; the caller maps them to the filesystem).
    """
    findings: list[Finding] = []
    for path, lines in sorted(added_lines.items()):
        if not path.endswith(".py"):
            continue
        for n in lines:
            line = read_line(path, n)
            if line is None:
                continue
            for name, pattern in SUPPRESSION_PATTERNS:
                if pattern.search(line):
                    findings.append(
                        Finding(
                            path=path,
                            line=n,
                            rule=f"suppression/{name}",
                            message=f"suppression comment on an added line: {line.strip()[:120]}",
                        )
                    )
                    break  # one finding per line, first match wins
    return findings


def scan_diff(repo_root, diff) -> list[Finding]:
    """Convenience wrapper scanning a DiffModel against *repo_root*."""
    added: dict[str, list[int]] = {
        path: sorted(diff.added_line_numbers(path))
        for path in diff.added_lines
    }

    def read_line(path: str, n: int) -> str | None:
        try:
            with (repo_root / path).open("r", encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh, start=1):
                    if i == n:
                        return line
                    if i > n:
                        break
        except OSError:
            return None
        return None

    return scan_added_lines(added, read_line)
