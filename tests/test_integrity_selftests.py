"""v5.1 §18 integrity self-tests — "the cases that matter most".

They test that the system resists what it was built to resist
(protected-path modification, expired exceptions, allowlist tampering)
and that it does not cry wolf (a pyproject reformat with no policy
change must not fire — semantic hashing, v5.1 §11.2).
"""

from __future__ import annotations

import datetime as dt

from tests.conftest import run_git
from tests.fixtures.scratch import commit, make_repo
from tests.test_selftests import run_full

TODAY = dt.date.today()


def _allowlist(rule: str, *, expires: dt.date | None = None, reason: str | None = None,
               added_on: dt.date | None = None, path: str = "src/scratch/app.py") -> str:
    expires = expires or (TODAY + dt.timedelta(days=90))
    added_on = added_on or TODAY
    reason = reason or (
        "Legacy exception documented for adoption; tracked for removal."
    )
    return (
        "[[entry]]\n"
        f'rule = "{rule}"\n'
        f'path = "{path}"\n'
        f'reason = "{reason}"\n'
        'added_by = "human"\n'
        f'added_on = "{added_on.isoformat()}"\n'
        f'expires = "{expires.isoformat()}"\n'
    )


class TestIntegrityFires:
    def test_edit_to_quality_config(self, tmp_path):
        # §18: edit to .quality/config.toml → integrity check
        repo = make_repo(tmp_path / "cfg")
        cfg = (repo / ".quality" / "config.toml").read_text(encoding="utf-8")
        cfg = cfg.replace("c901_max = 10", "c901_max = 11")
        commit(repo, {".quality/config.toml": cfg}, "weaken threshold", branch="feature")
        code, report = run_full(repo)
        assert code == 1
        gate = report["gates"]["integrity"]
        assert gate["status"] == "fail"
        assert gate["mechanism"] == "absolute"
        assert ".quality/config.toml" in gate["detail"]

    def test_expired_allowlist_entry(self, tmp_path):
        # §18: expired allowlist entry → integrity check (absolute; not
        # overridable by the allowlist itself)
        repo = make_repo(tmp_path / "expired")
        content = _allowlist(
            "pyscn/unused-function",
            added_on=TODAY - dt.timedelta(days=200),
            expires=TODAY - dt.timedelta(days=20),
        )
        commit(repo, {".quality/allowlist.toml": content}, "stale allowlist", branch="feature")
        code, report = run_full(repo)
        assert code == 1
        gate = report["gates"]["integrity"]
        assert gate["status"] == "fail"
        assert "expired" in gate["detail"]

    def test_allowlist_entry_added_fires_regardless_of_added_by(self, tmp_path):
        # §18: allowlist entry added at all → integrity check — the
        # control is the protected path, not the added_by value (v5.1
        # §10: an agent can write added_by = "human" as easily as a
        # human can).
        for added_by in ("agent", "human"):
            repo = make_repo(tmp_path / f"al-{added_by}")
            content = _allowlist("pyscn/unused-function").replace(
                'added_by = "human"', f'added_by = "{added_by}"'
            )
            commit(repo, {".quality/allowlist.toml": content},
                   f"add entry as {added_by}", branch="feature")
            code, report = run_full(repo)
            gate = report["gates"]["integrity"]
            assert gate["status"] == "fail", (added_by, gate)
            assert ".quality/allowlist.toml" in gate["detail"]

    def test_baseline_regenerated_in_pr(self, tmp_path):
        # §18: baseline regenerated in a PR → integrity check. Tier 1
        # has no baseline command at all (v5.1 §4.3 "not an artifact"),
        # so the file can only appear by deliberate creation — which is
        # exactly what the integrity gate must catch.
        repo = make_repo(tmp_path / "baseline")
        commit(repo, {
            ".quality/baseline.json": '{"note": "regenerated"}\n',
        }, "regenerate baseline", branch="feature")
        code, report = run_full(repo)
        gate = report["gates"]["integrity"]
        assert gate["status"] == "fail"
        assert ".quality/baseline.json" in gate["detail"]

    def test_workflow_edit_fires(self, tmp_path):
        repo = make_repo(tmp_path / "wf")
        commit(repo, {
            ".github/workflows/aufsicht.yml": "name: aufsicht\non: push\n",
        }, "edit ci", branch="feature")
        code, report = run_full(repo)
        gate = report["gates"]["integrity"]
        assert gate["status"] == "fail"
        assert ".github/workflows/aufsicht.yml" in gate["detail"]


class TestIntegrityDoesNotCryWolf:
    def test_pyproject_reformatted_no_policy_change(self, tmp_path):
        # §18: pyproject reformatted, no policy change → integrity does
        # NOT fire (semantic hash, v5.1 §11.2). Reordering keys and
        # adding comments is cosmetic.
        repo = make_repo(tmp_path / "reformat")
        original = (repo / "pyproject.toml").read_text(encoding="utf-8")
        reformatted = (
            "# reformatted for readability\n"
            "[project]\n"
            "version = \"0.1.0\"\n"
            "name = \"scratch\"\n"
            "# a comment agents love to add\n"
            "requires-python = \">=3.11\"\n"
            "dependencies = []\n"
        )
        assert "dependencies" in reformatted
        commit(repo, {"pyproject.toml": reformatted}, "reformat only", branch="feature")
        code, report = run_full(repo)
        gate = report["gates"]["integrity"]
        assert gate["status"] == "pass", gate

    def test_semantic_hash_stable_under_key_order(self):
        from aufsicht.integrity import pyproject_section_hash

        import tempfile, pathlib

        with tempfile.TemporaryDirectory() as tmp:
            a = pathlib.Path(tmp) / "a.toml"
            b = pathlib.Path(tmp) / "b.toml"
            a.write_text("[tool.ruff]\nline-length = 100\nselect = [\"E\"]\n")
            b.write_text("# comment\n[tool.ruff]\nselect = [\"E\"]\nline-length = 100\n")
            assert pyproject_section_hash(a, "tool.ruff") == pyproject_section_hash(b, "tool.ruff")

    def test_semantic_hash_changes_under_policy_change(self):
        from aufsicht.integrity import pyproject_section_hash

        import tempfile, pathlib

        with tempfile.TemporaryDirectory() as tmp:
            a = pathlib.Path(tmp) / "a.toml"
            b = pathlib.Path(tmp) / "b.toml"
            a.write_text("[tool.ruff]\nline-length = 100\n")
            b.write_text("[tool.ruff]\nline-length = 88\n")
            assert pyproject_section_hash(a, "tool.ruff") != pyproject_section_hash(b, "tool.ruff")

    def test_policy_section_change_fires_via_config_hashes(self, tmp_path):
        # The §11.2 fallback: quality config that must live in
        # pyproject is protected by a semantic hash recorded in
        # .quality/config-hashes.json (itself protected).
        import hashlib, json

        repo = make_repo(tmp_path / "sect")
        pyproject = (repo / "pyproject.toml").read_text(encoding="utf-8")
        from aufsicht.integrity import pyproject_section_hash

        (repo / "pyproject.toml").write_text(
            pyproject + '\n[tool.ruff]\nline-length = 100\n', encoding="utf-8"
        )
        recorded = pyproject_section_hash(repo / "pyproject.toml", "tool.ruff")
        (repo / "pyproject.toml").write_text(pyproject, encoding="utf-8")  # restore
        config_hashes = {"tool.ruff": recorded}
        commit(repo, {
            ".quality/config-hashes.json": json.dumps(config_hashes, indent=2) + "\n",
        }, "record section hash", branch="feature")
        # Now weaken the policy in pyproject (path-level diff sees only
        # pyproject, which is NOT protected — the semantic hash must
        # catch it).
        weakened = (repo / "pyproject.toml").read_text(encoding="utf-8")
        weakened += '\n[tool.ruff]\nline-length = 200\n'
        commit(repo, {"pyproject.toml": weakened}, "weaken via pyproject")
        code, report = run_full(repo)
        gate = report["gates"]["integrity"]
        assert gate["status"] == "fail", gate
        assert "tool.ruff" in gate["detail"]
