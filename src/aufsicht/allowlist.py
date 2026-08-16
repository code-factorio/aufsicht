"""The escape valve (v5.1 §10): `.quality/allowlist.toml`.

Exactly one sanctioned mechanism. Without it, agents invent an
unsanctioned one, and that one is not reviewable.

* CI fails on any entry past `expires`. No silent permanence.
* Maximum lifetime 180 days; cycle entries 90 days (§10.1).
* Empty `reason`, or under 20 characters, fails validation.
* `added_by` is provenance, not enforcement — an agent can write
  `added_by = "human"` as easily as a human can.

The actual control is that this file is a protected path (§11): an
agent cannot add an entry because it cannot merge a change to the
file, not because of anything it writes inside it.
"""

from __future__ import annotations

import datetime as _dt
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .errors import ToolingError

ALLOWLIST_PATH = ".quality/allowlist.toml"

MAX_LIFETIME_DAYS = 180
CYCLE_MAX_LIFETIME_DAYS = 90
MIN_REASON_CHARS = 20


def _parse_date(value: str) -> _dt.date:
    return _dt.date.fromisoformat(value)


@dataclass(frozen=True)
class AllowlistEntry:
    rule: str                    # e.g. "pip-audit/GHSA-...", "cycle/...", "pyscn/deadcode/..."
    path: str | None = None
    reason: str = ""
    added_by: str = "unknown"
    added_on: str = ""
    expires: str = ""            # ISO date
    aliases: tuple[str, ...] = ()  # every known vuln id (GHSA, PYSEC, CVE)
    fingerprint: str | None = None  # Tier 2 only

    @property
    def expires_date(self) -> _dt.date | None:
        return _parse_date(self.expires) if self.expires else None

    def expiry_state(self, today: _dt.date) -> str:
        """'ok' | 'expired' | 'expiring-soon' (within 30 days)."""
        exp = self.expires_date
        if exp is None:
            return "ok"
        if exp < today:
            return "expired"
        if (exp - today).days <= 30:
            return "expiring-soon"
        return "ok"

    def lifetime_days(self) -> int | None:
        if not self.added_on or not self.expires:
            return None
        return (_parse_date(self.expires) - _parse_date(self.added_on)).days


@dataclass(frozen=True)
class Allowlist:
    entries: tuple[AllowlistEntry, ...]

    def matching(self, rule_prefix: str) -> list[AllowlistEntry]:
        """Entries whose rule equals or falls under *rule_prefix*."""
        prefix = rule_prefix.rstrip("/") + "/"
        return [
            e for e in self.entries
            if e.rule == rule_prefix or e.rule.startswith(prefix)
        ]

    def expiring_within_30d(self, today: _dt.date) -> list[str]:
        keys = []
        for e in self.entries:
            if e.fingerprint:
                keys.append(e.fingerprint)
            elif e.expires_date and 0 <= (e.expires_date - today).days <= 30:
                keys.append(f"{e.rule}:{e.path}")
        return keys


def load_allowlist(repo: Path) -> Allowlist:
    path = repo / ALLOWLIST_PATH
    if not path.is_file():
        return Allowlist(entries=())
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise ToolingError(f"cannot parse {ALLOWLIST_PATH}: {exc}") from exc
    entries: list[AllowlistEntry] = []
    for raw in data.get("entry", []):
        entry = AllowlistEntry(
            rule=str(raw.get("rule", "")),
            path=raw.get("path"),
            reason=str(raw.get("reason", "")),
            added_by=str(raw.get("added_by", "unknown")),
            added_on=str(raw.get("added_on", "")),
            expires=str(raw.get("expires", "")),
            aliases=tuple(str(a) for a in raw.get("aliases", ())),
            fingerprint=raw.get("fingerprint"),
        )
        entries.append(entry)
    return Allowlist(entries=tuple(entries))


def validate(entries: tuple[AllowlistEntry, ...], today: _dt.date) -> list[str]:
    """Structural validation problems (v5.1 §10). Non-empty result →
    the integrity gate fails."""
    problems: list[str] = []
    for i, e in enumerate(entries):
        where = f"entry[{i}] rule={e.rule!r}"
        if not e.rule:
            problems.append(f"{where}: missing rule")
        if len(e.reason.strip()) < MIN_REASON_CHARS:
            problems.append(
                f"{where}: reason must be at least {MIN_REASON_CHARS} characters"
            )
        try:
            _parse_date(e.expires)
        except ValueError:
            problems.append(f"{where}: expires must be an ISO date")
            continue
        try:
            _parse_date(e.added_on)
        except ValueError:
            problems.append(f"{where}: added_on must be an ISO date")
            continue
        lifetime = e.lifetime_days()
        limit = (
            CYCLE_MAX_LIFETIME_DAYS
            if e.rule.startswith("cycle/")
            else MAX_LIFETIME_DAYS
        )
        if lifetime is not None and lifetime > limit:
            problems.append(
                f"{where}: lifetime {lifetime}d exceeds the {limit}d maximum "
                "(cycles: 90d, v5.1 §10.1)"
            )
    return problems


def expired_entries(entries: tuple[AllowlistEntry, ...], today: _dt.date) -> list[AllowlistEntry]:
    return [e for e in entries if e.expiry_state(today) == "expired"]


def ignore_vuln_flags(entries: tuple[AllowlistEntry, ...]) -> list[str]:
    """One --ignore-vuln flag per allowlisted vulnerability, covering
    every known alias (v5.1 §10.1 — an ignore keyed on the wrong alias
    silently stops matching after an advisory DB update)."""
    flags: list[str] = []
    for e in entries:
        if not e.rule.startswith("pip-audit/"):
            continue
        ids = [e.rule[len("pip-audit/"):]] + [a for a in e.aliases if a != e.rule[len("pip-audit/") :]]
        for vuln_id in dict.fromkeys(ids):  # dedupe, keep order
            flag = f"--ignore-vuln={vuln_id}"
            if flag not in flags:
                flags.append(flag)
    return flags


def canonicalize_cycle(modules: list[str]) -> tuple[tuple[str, ...], str]:
    """Rotate to start at the lexicographically smallest member, keep
    direction, hash (v5.1 §10.1). a→b→c→a and b→c→a→b are the same
    cycle with different serialisations; without canonicalisation,
    allowlist entries stop matching between runs and the exception
    silently expires."""
    if not modules:
        return ((), "")
    smallest_idx = min(range(len(modules)), key=lambda i: modules[i])
    rotated = tuple(modules[smallest_idx:] + modules[:smallest_idx])
    import hashlib

    digest = hashlib.sha256("\n".join(rotated).encode("utf-8")).hexdigest()
    return rotated, digest
