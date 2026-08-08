"""Campaign preflight + working-tree residue regression tests.

Covers the snaplink-console B6-advance lessons (2026-08-08):
- validator-baseline preflight: repo-scoped pipeline validators red at
  HEAD made every implement stage fail closed regardless of agent
  quality (two full campaign rounds burned); preflight surfaces the
  baseline BEFORE pipelines run
- working-tree residue: pipeline stage agents left 20-30 uncommitted
  files per round with no campaign signal; the residue report names
  them at campaign end
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from pbatch.campaign import CampaignContext
from pbatch.campaign_exec import _preflight_validators, _report_working_tree_residue
from pbatch.campaign_models import CampaignSettings
from pbatch.campaign_state import StateStore
from pbatch.config import ValidatorSpec

TEMPLATE = """\
git_commit: false
stages:
  - name: implement
    from_prompt: x
    output: out.md
    validate: {validate}
    tasks:
      - prompt: x
        output: out.md
"""


@pytest.fixture(autouse=True)
def _capture_info(caplog):
    """Preflight/report log at INFO; caplog defaults to WARNING-only."""
    caplog.set_level(0)  # capture everything, including INFO


def _make_context(root: Path, template: str, preflight_strict: bool = False):
    settings = CampaignSettings(pipeline_template=template)
    args = argparse.Namespace(preflight_strict=preflight_strict)
    return CampaignContext(
        root=root,
        settings=settings,
        args=args,
        store=StateStore(root / "docs/auto/state.jsonl", 1024),
        snapshot={},
    )


def _write_template(tmp_path: Path, validate: str) -> Path:
    template = tmp_path / "pipeline.yaml"
    template.write_text(TEMPLATE.format(validate=validate), encoding="utf-8")
    return template


@pytest.fixture()
def repo_validators(monkeypatch):
    """Deterministic repo-scoped validators (no pi-batch.yaml dependence)."""
    from pbatch import config as pbatch_config

    monkeypatch.setitem(
        pbatch_config.VALIDATOR_SPECS,
        "testrepo_ok",
        ValidatorSpec(cmd="exit 0", scope="repo"),
    )
    monkeypatch.setitem(
        pbatch_config.VALIDATOR_SPECS,
        "testrepo_fail",
        ValidatorSpec(cmd="exit 3", scope="repo"),
    )
    monkeypatch.setitem(
        pbatch_config.VALIDATOR_SPECS,
        "testfile_red",
        ValidatorSpec(cmd="exit 1", scope="file"),
    )
    monkeypatch.setitem(
        pbatch_config.VALIDATOR_SPECS,
        "testjudge_red",
        ValidatorSpec(cmd="exit 1", scope="repo", judge=True),
    )


def test_preflight_reports_baseline_failures_without_aborting(
    tmp_path, repo_validators, caplog
):
    _write_template(tmp_path, "testrepo_ok,testrepo_fail")
    context = _make_context(tmp_path, "pipeline.yaml")
    failures = _preflight_validators(context)
    assert failures == 1  # testrepo_fail red on baseline; testrepo_ok green
    assert "PREFLIGHT: testrepo_ok OK" in caplog.text
    assert "PREFLIGHT: testrepo_fail FAILED on baseline" in caplog.text
    assert "implement stages will fail closed on this baseline" in caplog.text


def test_preflight_strict_aborts_before_pipelines(tmp_path, repo_validators):
    _write_template(tmp_path, "testrepo_fail")
    context = _make_context(tmp_path, "pipeline.yaml", preflight_strict=True)
    with pytest.raises(SystemExit) as exc:
        _preflight_validators(context)
    assert exc.value.code == 4


def test_preflight_skips_file_scoped_and_judge_validators(
    tmp_path, repo_validators, caplog
):
    _write_template(tmp_path, "testfile_red,testjudge_red")
    context = _make_context(tmp_path, "pipeline.yaml")
    failures = _preflight_validators(context)
    assert failures == 0
    assert "no repo-scoped validators" in caplog.text.lower() or "skipped" in caplog.text


def test_preflight_no_validators_is_noop(tmp_path, caplog):
    _write_template(tmp_path, "")
    context = _make_context(tmp_path, "pipeline.yaml")
    assert _preflight_validators(context) == 0
    assert "no repo-scoped validators" in caplog.text


def test_preflight_substitutes_cwd_placeholder(tmp_path, monkeypatch):
    from pbatch import config as pbatch_config

    marker = tmp_path / "marker"
    monkeypatch.setitem(
        pbatch_config.VALIDATOR_SPECS,
        "testrepo_cwd",
        ValidatorSpec(cmd="touch {cwd}/marker", scope="repo"),
    )
    _write_template(tmp_path, "testrepo_cwd")
    context = _make_context(tmp_path, "pipeline.yaml")
    assert _preflight_validators(context) == 0
    assert marker.exists()


def _git(tmp_path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(tmp_path), *args],
        check=True,
        capture_output=True,
        text=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
             "PATH": "/usr/bin:/bin"},
    )


def test_working_tree_residue_reported(tmp_path, caplog):
    _git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-qm", "init")
    (tmp_path / "a.txt").write_text("two\n", encoding="utf-8")  # modified
    (tmp_path / "b.txt").write_text("x", encoding="utf-8")      # untracked
    _report_working_tree_residue(_make_context(tmp_path, "pipeline.yaml"))
    assert "2 uncommitted file(s)" in caplog.text
    assert "1 modified" in caplog.text
    assert "1 untracked" in caplog.text
    assert "a.txt" in caplog.text
    assert "b.txt" in caplog.text


def test_working_tree_clean_reports_nothing(tmp_path, caplog):
    _git(tmp_path, "init", "-q")
    _report_working_tree_residue(_make_context(tmp_path, "pipeline.yaml"))
    assert "WORKTREE: clean" in caplog.text


def test_working_tree_residue_non_git_dir_is_silent(tmp_path, caplog):
    _report_working_tree_residue(_make_context(tmp_path, "pipeline.yaml"))
    assert "WORKTREE: clean" in caplog.text
