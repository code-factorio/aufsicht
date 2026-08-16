"""The one range-intersection filter (v5.1 §4.2 — "one filter, not
three").

Every diff-scoped gate configures this predicate per gate; no adapter
implements its own notion of "is this finding new":

```python
def in_scope(finding, changed_files, added_lines, mode):
    if mode == "file":     return finding.path in changed_files
    if mode == "line":     return bool(range(finding.start, finding.end+1) & added_lines)
    if mode == "function": return bool(finding.span & added_lines)  # requires a span
```
"""

from __future__ import annotations

from .model import DiffModel, Finding, ScopeMode


def in_scope(finding: Finding, diff: DiffModel, mode: ScopeMode) -> bool:
    if mode is ScopeMode.FILE:
        return finding.path in diff.changed_files
    if mode is ScopeMode.LINE:
        start, end = finding.span
        return diff.ranges_intersect(finding.path, start, end)
    if mode is ScopeMode.FUNCTION:
        # Requires a finding that carries a function-level span. Tier 1
        # has no diff→AST mapper (v5.1 §4.2), so the span is whatever
        # the analyser's end_location covers — which for C901 is the
        # def line only (see probe_facts.C901_SCOPE).
        start, end = finding.span
        return diff.ranges_intersect(finding.path, start, end)
    raise ValueError(f"unknown scope mode: {mode!r}")
