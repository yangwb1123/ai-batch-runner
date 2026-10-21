"""Real Git worktree -> owned child -> cutoff receipt -> coordinator ledger."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

from pbatch import campaign_exec, campaign_pipeline, config
from pbatch.campaign_models import CampaignSettings, Direction, safe_slug
from pbatch.campaign_state import StateStore


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _fixture_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Deadline Fixture")
    _git(root, "config", "user.email", "deadline@example.invalid")
    _git(root, "config", "core.hooksPath", "/dev/null")
    _git(root, "config", "commit.gpgSign", "false")
    (root / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
    (root / "worker.txt").write_text("before\n", encoding="utf-8")
    (root / "analysis.json").write_text("[]\n", encoding="utf-8")
    (root / "pipeline.yaml").write_text(
        "stages:\n  - name: requirements\n    from_prompt: fixture\n    output: report.md\n",
        encoding="utf-8")
    (root / "pi-batch.py").write_text(
        "import pathlib,sys,time\n"
        "pathlib.Path('worker.txt').write_text('started\\n')\n"
        "log=pathlib.Path(sys.argv[sys.argv.index('--log-file')+1])\n"
        "while True:\n"
        "    log.write_text(str(time.time()))\n"
        "    time.sleep(0.02)\n", encoding="utf-8")
    origin = time.time() - 30
    (root / "state.jsonl").write_text(json.dumps({
        "status": "CAMPAIGN_RUN_STARTED", "updated_at": str(origin)}) + "\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "private fixture baseline")
    return root, _git(root, "rev-parse", "HEAD")


def test_expiring_batch_preserves_first_attempt_and_never_starts_queued_work(monkeypatch, tmp_path):
    root, baseline = _fixture_repo(tmp_path)
    monkeypatch.setattr(config, "TOOL_ROOT", str(root))
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr("pbatch.memory.record_campaign", lambda *a: None)
    settings = CampaignSettings(
        pipeline_template="pipeline.yaml", state_file="state.jsonl", worktree_root=".worktrees",
        budget_seconds=33, pipeline_timeout=10, pipeline_concurrency=2,
        recovery_enabled=True, recovery_restore_on_failure=True)
    args = SimpleNamespace(agent_bin="pi", memory_mode="off", stream_output="none",
                           model="", provider="", thinking="", timeout=0, auto_merge=False)
    context = SimpleNamespace(root=root, settings=settings, args=args,
                              store=StateStore(root / "state.jsonl"), snapshot={}, run_id="fixture")
    items = [(Direction(module="src", title=title), root / "analysis.json", "fp-" + title)
             for title in ("first", "queued")]
    outcomes, elapsed = campaign_exec._execute_pipeline_batch(context, items, 1)
    by_title = {item.direction.title: item for item in outcomes}
    assert {item.status for item in outcomes} == {"BUDGET_BLOCKED"}
    first, queued = by_title["first"], by_title["queued"]
    assert first.recovery_status == "PRESERVED"
    assert first.elapsed > 0 and elapsed < 8
    assert (Path(first.evidence[0]) / "worker.txt").read_text() == "started\n"
    queued_tree = (settings.effective_worktree_root(root) / safe_slug(settings.name)
                   / queued.direction.direction_id)
    assert queued.branch and queued.baseline_commit == baseline
    assert (queued_tree / "worker.txt").read_text() == "before\n"
    assert _git(queued_tree, "rev-parse", "HEAD") == baseline
    assert _git(root, "rev-parse", "HEAD") == baseline
    assert _git(Path(first.evidence[0]), "rev-parse", "HEAD") == baseline
    assert first.dirty_evidence
    assert context.round_progress == 0 and context.recovery_blocked
    for direction, _analysis, _fp in items:
        row = context.store.latest_event(direction.module, direction.direction_id)
        assert row["status"] == "BUDGET_BLOCKED"
    receipts = [Path(path) for path in first.evidence if path.endswith(".json")
                and "deadline-receipts" in path]
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["child_returncode"] is not None
    assert receipt["cutoff_epoch"] < time.time()
