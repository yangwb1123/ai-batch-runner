"""Regression tests for ai/run-review.py.

Cover the repaired behaviors: live-run output persistence (including partial
output on failure), --all stage output chaining with explicit-value
precedence, and neutral defaults for omitted context fields.

The runner is loaded through importlib because its filename contains dashes.
"""

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

RUN_REVIEW = Path(__file__).resolve().parent.parent / "ai" / "run-review.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("run_review_under_test", RUN_REVIEW)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclass and module internals need the module registered
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def fake_agent(tmp_path):
    """A fake agent CLI that counts chained-stage markers in its prompt.

    Emitting a small deterministic value keeps chained prompts small (echoing
    the full prompt back would embed every upstream prompt into the next
    stage's output and grow the argv exponentially across --all stages). The
    count of "--- Stage NN output ---" markers verifies chaining end to end.
    """
    path = tmp_path / "fake-agent.sh"
    path.write_text(
        "#!/bin/sh\n"
        "echo \"$2\" | grep -o -- '--- Stage [0-9][0-9] output ---' | wc -l\n"
    )
    path.chmod(0o755)
    return path


class _Args:
    def __init__(self, **kwargs):
        self.agent_bin = kwargs.get("agent_bin", "")
        self.model = kwargs.get("model", "")
        self.output_dir = kwargs.get("output_dir")
        self.context_name = kwargs.get("context_name", "ctx")
        self.repo = kwargs.get("repo")
        self.timeout = kwargs.get("timeout", 0)
        self.validate_cmd = kwargs.get("validate_cmd", "")


def test_chain_variables_fills_schema_placeholder():
    mod = load_runner()
    prior = {"00": "DISCOVERY TEXT"}
    variables = {"PRODUCT_DISCOVERY_OUTPUT": "(paste Stage 00 output, or 'N/A')"}
    defaults = {"PRODUCT_DISCOVERY_OUTPUT": "(paste Stage 00 output, or 'N/A')"}
    chained = mod.chain_variables(prior, "01", variables, defaults)
    assert chained["PRODUCT_DISCOVERY_OUTPUT"] == "--- Stage 00 output ---\nDISCOVERY TEXT"


def test_chain_variables_respects_explicit_value():
    mod = load_runner()
    prior = {"00": "DISCOVERY TEXT"}
    variables = {"PRODUCT_DISCOVERY_OUTPUT": "explicit context value"}
    defaults = {"PRODUCT_DISCOVERY_OUTPUT": "(paste Stage 00 output, or 'N/A')"}
    chained = mod.chain_variables(prior, "01", variables, defaults)
    assert chained == {}


def test_chain_variables_skips_missing_sources():
    mod = load_runner()
    variables = {"PRIOR_FINDINGS": "(paste Critical/High findings from Stages 01-03, or 'N/A')"}
    defaults = dict(variables)
    chained = mod.chain_variables({}, "04", variables, defaults)
    assert chained == {}


def test_chain_variables_bounds_total_chained_bytes(monkeypatch, tmp_path):
    """Large history is head-bounded while every source stays identifiable."""
    mod = load_runner()
    monkeypatch.setattr(mod.batch_config, "EVIDENCE_MAX_BYTES", 4096)
    monkeypatch.setattr(mod.batch_config, "PROMPT_MAX_BYTES", 122880)
    prior = {f"{index:02d}": f"stage-{index}:" + "x" * 40000
             for index in range(9)}
    variables = {"ALL_PRIOR_FINDINGS_SUMMARY": "(paste prior findings)"}
    defaults = dict(variables)
    value = mod.chain_variables(
        prior, "09", variables, defaults,
        paths={stage: tmp_path / f"stage-{stage}.out.md" for stage in prior},
    )["ALL_PRIOR_FINDINGS_SUMMARY"]

    source_parts = re.findall(
        r"--- Stage [0-9]{2} output ---\n(.*?)(?=\n\[stage [0-9]{2} output truncated:"
        r"|\n\n--- Stage [0-9]{2} output ---|\n\n<chained-sources|$)",
        value, re.DOTALL,
    )
    assert sum(len(part.encode("utf-8")) for part in source_parts) <= 4096
    assert value.count("--- Stage ") == 9
    assert "truncated" in value
    assert "full output:" in value
    assert "stage-0:" + "x" * 100 in value


def test_chained_omission_marker_when_budget_below_source_count(monkeypatch):
    mod = load_runner()
    monkeypatch.setattr(mod.batch_config, "EVIDENCE_MAX_BYTES", 3)
    monkeypatch.setattr(mod.batch_config, "PROMPT_MAX_BYTES", 122880)
    prior = {f"{index:02d}": "x" * 40 for index in range(9)}
    variables = {"ALL_PRIOR_FINDINGS_SUMMARY": "(paste prior findings)"}
    value = mod.chain_variables(prior, "09", variables, dict(variables))[
        "ALL_PRIOR_FINDINGS_SUMMARY"]
    assert value.count("--- Stage ") == 3
    assert '<chained-sources-omitted count="6" />' in value


def test_all_bounds_chained_outputs_before_prompt_gate(tmp_path):
    """A realistic ten-stage run keeps decision prompts below argv limits."""
    agent = tmp_path / "large-review-agent.sh"
    agent.write_text(
        "#!/bin/sh\n"
        "stage=$(printf '%s' \"$2\" | sed -n 's/^# Stage \\([0-9][0-9]\\):.*/\\1/p' | head -n 1)\n"
        "printf '%s' \"$2\" > \"$PROMPT_DIR/prompt-stage-${stage}.txt\"\n"
        f"{sys.executable} -c 'import sys; sys.stdout.write(\"x\" * 30000)'\n",
        encoding="utf-8",
    )
    agent.chmod(0o755)
    ctx = tmp_path / "ctx.yaml"
    ctx.write_text("project: Test\nsubsystem: Large chain\n", encoding="utf-8")
    out_dir = tmp_path / "reviews"
    env = os.environ.copy()
    env["PROMPT_DIR"] = str(out_dir)
    result = subprocess.run(
        [sys.executable, str(RUN_REVIEW), "--all", "--context", str(ctx),
         "--agent-bin", str(agent), "--output-dir", str(out_dir),
         "--repo", str(tmp_path)],
        capture_output=True, text=True, timeout=120, env=env,
    )
    assert result.returncode == 0, result.stderr
    for stage in ("06", "07", "09"):
        prompt = (out_dir / f"prompt-stage-{stage}.txt").read_bytes()
        assert len(prompt) <= mod_prompt_limit()
        assert b"truncated" in prompt
        assert (out_dir / f"stage-{stage}.out.md").exists()


def mod_prompt_limit():
    """Read the same configured limit used by the subprocess runner."""
    mod = load_runner()
    return mod.batch_config.PROMPT_MAX_BYTES


def test_context_to_vars_neutral_defaults():
    """Omitted storage/team/sprint fields must not fabricate project facts."""
    mod = load_runner()
    v03 = mod.context_to_vars({}, "03")
    assert v03["STORAGE_SUMMARY"] == ""
    v07 = mod.context_to_vars({}, "07")
    assert v07["TEAM_SIZE"] == ""
    assert v07["SPRINT_DURATION"] == ""
    v09 = mod.context_to_vars({}, "09")
    assert v09["TEAM_SIZE"] == ""


def test_run_stage_persists_output(tmp_path, fake_agent):
    mod = load_runner()
    args = _Args(agent_bin=str(fake_agent), output_dir=str(tmp_path), repo=str(tmp_path))
    rc = mod.run_stage("00", "PROMPT BODY", args)
    assert rc == 0
    out = tmp_path / "stage-00.out.md"
    assert out.exists()
    assert out.read_text(encoding="utf-8").strip() == "0"


def test_run_stage_does_not_save_on_failure(tmp_path):
    """A failing agent must not leave a stage-NN.out.md behind."""
    agent = tmp_path / "fail-agent.sh"
    agent.write_text("#!/bin/sh\necho \"partial evidence\"\nexit 3\n")
    agent.chmod(0o755)
    mod = load_runner()
    args = _Args(agent_bin=str(agent), output_dir=str(tmp_path), repo=str(tmp_path))
    rc = mod.run_stage("00", "PROMPT BODY", args)
    assert rc == 1
    assert not (tmp_path / "stage-00.out.md").exists()


def test_run_stage_rejects_provider_error_output(tmp_path):
    """Exit 0 with a provider failure signature (rate limit) must not save."""
    agent = tmp_path / "ratelimit-agent.sh"
    agent.write_text("#!/bin/sh\necho \"Error: rate_limit_error, retry later\"\n")
    agent.chmod(0o755)
    mod = load_runner()
    args = _Args(agent_bin=str(agent), output_dir=str(tmp_path), repo=str(tmp_path))
    rc = mod.run_stage("00", "PROMPT BODY", args)
    assert rc == 1
    assert not (tmp_path / "stage-00.out.md").exists()


def test_review_runner_uses_shared_agent_error_policy(monkeypatch):
    """Review runs honor the same per-agent signatures as pbatch tasks."""
    import re

    mod = load_runner()
    monkeypatch.setattr(
        mod.batch_config, "agent_error_patterns",
        lambda name="": (re.compile(r"provider_down"),),
    )
    assert mod.agent_failure_reason(0, "provider_down", "custom-agent")
    assert not mod.agent_failure_reason(0, "rate_limit_error", "custom-agent")


def test_run_stage_rejects_quota_output(tmp_path):
    agent = tmp_path / "quota-agent.sh"
    agent.write_text("#!/bin/sh\necho \"insufficient_quota: API credits exhausted\"\n")
    agent.chmod(0o755)
    mod = load_runner()
    args = _Args(agent_bin=str(agent), output_dir=str(tmp_path), repo=str(tmp_path))
    rc = mod.run_stage("00", "PROMPT BODY", args)
    assert rc == 1
    assert not (tmp_path / "stage-00.out.md").exists()


def test_run_stage_rejects_empty_output(tmp_path):
    agent = tmp_path / "empty-agent.sh"
    agent.write_text("#!/bin/sh\n")
    agent.chmod(0o755)
    mod = load_runner()
    args = _Args(agent_bin=str(agent), output_dir=str(tmp_path), repo=str(tmp_path))
    rc = mod.run_stage("00", "PROMPT BODY", args)
    assert rc == 1
    assert not (tmp_path / "stage-00.out.md").exists()


def test_run_stage_rejects_offline_output(tmp_path):
    """Offline/network failure replies (curl banner, DNS, refused) must not save."""
    for banner in (
        "curl: (7) Failed to connect to api.example.com port 443",
        "getaddrinfo: Name or service not known",
        "ConnectionError: network is unreachable",
        "connect: No route to host",
    ):
        agent = tmp_path / "offline-agent.sh"
        agent.write_text(f"#!/bin/sh\necho \"{banner}\"\n")
        agent.chmod(0o755)
        mod = load_runner()
        args = _Args(agent_bin=str(agent), output_dir=str(tmp_path), repo=str(tmp_path))
        assert mod.run_stage("00", "PROMPT BODY", args) == 1
        assert not (tmp_path / "stage-00.out.md").exists()


def test_run_stage_rejects_timeout(tmp_path):
    """A hung agent (e.g. offline machine) must be killed at the deadline and
    leave no output file."""
    agent = tmp_path / "hung-agent.sh"
    agent.write_text("#!/bin/sh\nsleep 30\n")
    agent.chmod(0o755)
    mod = load_runner()
    args = _Args(agent_bin=str(agent), output_dir=str(tmp_path), repo=str(tmp_path), timeout=1)
    rc = mod.run_stage("00", "PROMPT BODY", args)
    assert rc == 1
    assert not (tmp_path / "stage-00.out.md").exists()

    args.timeout = -1
    assert mod.run_stage("00", "PROMPT BODY", args) == 1
    assert not (tmp_path / "stage-00.out.md").exists()

    old_limit = mod.batch_config.PROMPT_MAX_BYTES
    mod.batch_config.PROMPT_MAX_BYTES = 1
    try:
        args.timeout = 0
        assert mod.run_stage("00", "PROMPT BODY", args) == 1
    finally:
        mod.batch_config.PROMPT_MAX_BYTES = old_limit


def test_run_stage_saves_legitimate_mention_of_error_words(tmp_path):
    """Review prose discussing timeouts or unauthorized responses must not be
    misclassified as a provider failure."""
    agent = tmp_path / "prose-agent.sh"
    agent.write_text(
        "#!/bin/sh\n"
        "echo 'The flow relies on timeout handling and returns 401 Unauthorized; "
        "quota is not enforced here.'\n"
    )
    agent.chmod(0o755)
    mod = load_runner()
    args = _Args(agent_bin=str(agent), output_dir=str(tmp_path), repo=str(tmp_path))
    rc = mod.run_stage("00", "PROMPT BODY", args)
    assert rc == 0
    assert (tmp_path / "stage-00.out.md").exists()


def test_run_stage_rejects_bounded_oversized_output(monkeypatch, tmp_path):
    """The review compatibility runner must not accumulate an unlimited message."""
    agent = tmp_path / "noisy-agent.sh"
    agent.write_text("#!/bin/sh\nprintf '012345678901234567890123456789'\n")
    agent.chmod(0o755)
    mod = load_runner()
    monkeypatch.setattr(mod.batch_config, "OUTPUT_MAX_BYTES", 16)
    args = _Args(agent_bin=str(agent), output_dir=str(tmp_path), repo=str(tmp_path))
    rc = mod.run_stage("00", "PROMPT BODY", args)
    assert rc == 1
    assert not (tmp_path / "stage-00.out.md").exists()


def test_all_chains_stage_outputs_end_to_end(tmp_path, fake_agent):
    """--all must persist every stage and chain stage outputs downstream.

    The fake agent counts chained markers in the rendered prompt, so each
    stage-NN.out.md holds the number of upstream outputs it received: stage
    01 gets stage 00, stage 04 gets stages 01-03, stage 07 gets findings
    00-06 plus the stage 01 architecture, stage 09 gets stages 00-08.
    """
    ctx = tmp_path / "ctx.yaml"
    ctx.write_text("project: Test\nsubsystem: Chain\n", encoding="utf-8")
    out_dir = tmp_path / "reviews"
    result = subprocess.run(
        [
            sys.executable, str(RUN_REVIEW),
            "--all", "--context", str(ctx),
            "--agent-bin", str(fake_agent),
            "--output-dir", str(out_dir),
            "--repo", str(tmp_path),
        ],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    outputs = sorted(p.name for p in out_dir.iterdir())
    assert len(outputs) == 10
    assert (out_dir / "stage-00.out.md").read_text(encoding="utf-8").strip() == "0"
    assert (out_dir / "stage-01.out.md").read_text(encoding="utf-8").strip() == "1"
    assert (out_dir / "stage-02.out.md").read_text(encoding="utf-8").strip() == "1"
    assert (out_dir / "stage-03.out.md").read_text(encoding="utf-8").strip() == "1"
    assert (out_dir / "stage-04.out.md").read_text(encoding="utf-8").strip() == "3"
    assert (out_dir / "stage-06.out.md").read_text(encoding="utf-8").strip() == "4"
    assert (out_dir / "stage-07.out.md").read_text(encoding="utf-8").strip() == "8"
    assert (out_dir / "stage-09.out.md").read_text(encoding="utf-8").strip() == "9"


def test_resume_skips_completed_stages(tmp_path):
    """--resume must not call the agent again for stages with saved output."""
    counter = tmp_path / "counter"
    agent = tmp_path / "counter-agent.sh"
    agent.write_text(
        f"#!/bin/sh\necho x >> {counter}\necho \"OK\"\n"
    )
    agent.chmod(0o755)
    ctx = tmp_path / "ctx.yaml"
    ctx.write_text("project: Test\nsubsystem: Chain\n", encoding="utf-8")
    out_dir = tmp_path / "reviews"
    cmd = [
        sys.executable, str(RUN_REVIEW),
        "--all", "--context", str(ctx),
        "--agent-bin", str(agent),
        "--output-dir", str(out_dir),
        "--repo", str(tmp_path),
    ]
    first = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert first.returncode == 0, first.stderr
    calls_after_first = counter.read_text(encoding="utf-8").count("x")
    assert calls_after_first == 10

    second = subprocess.run(cmd + ["--resume"], capture_output=True, text=True, timeout=120)
    assert second.returncode == 0, second.stderr
    assert "SKIP" in second.stdout
    calls_after_resume = counter.read_text(encoding="utf-8").count("x")
    assert calls_after_resume == 10  # no agent call on resume
    assert len(list(out_dir.iterdir())) == 10

    # A new/current validator is part of the reuse fingerprint in practice:
    # old outputs that fail it must regenerate instead of being promoted by
    # file existence alone.
    fresh_agent = tmp_path / "fresh-agent.sh"
    fresh_agent.write_text("#!/bin/sh\necho FRESH\n")
    fresh_agent.chmod(0o755)
    fresh_cmd = list(cmd)
    fresh_cmd[fresh_cmd.index("--agent-bin") + 1] = str(fresh_agent)
    third = subprocess.run(
        fresh_cmd + ["--resume", "--validate-cmd", "grep -q FRESH {output}"],
        capture_output=True, text=True, timeout=120)
    assert third.returncode == 0, third.stderr
    assert "REGENERATE" in third.stderr
    assert third.stdout.count("SKIP") == 0
    assert all(path.read_text(encoding="utf-8").strip() == "FRESH"
               for path in out_dir.glob("stage-*.out.md"))


def test_resume_reruns_failed_stage_with_disk_chaining(tmp_path):
    """A stage rejected in the previous session reruns on --resume and chains
    from outputs saved on disk; completed stages are not rerun."""
    counter = tmp_path / "counter"
    rejecting = tmp_path / "rejecting-agent.sh"
    rejecting.write_text(
        "#!/bin/sh\n"
        f"echo x >> {counter}\n"
        "case \"$2\" in\n"
        "  *\"# Stage 02\"*) echo \"Error: rate_limit_error\" ; exit 0 ;;\n"
        "  *) echo \"OK\" ;;\n"
        "esac\n"
    )
    rejecting.chmod(0o755)
    ctx = tmp_path / "ctx.yaml"
    ctx.write_text("project: Test\nsubsystem: Chain\n", encoding="utf-8")
    out_dir = tmp_path / "reviews"
    base = [
        sys.executable, str(RUN_REVIEW),
        "--all", "--context", str(ctx),
        "--output-dir", str(out_dir),
        "--repo", str(tmp_path),
    ]
    first = subprocess.run(base + ["--agent-bin", str(rejecting)], capture_output=True, text=True, timeout=120)
    assert first.returncode != 0  # stage 02 rejected
    outputs = sorted(p.name for p in out_dir.iterdir())
    assert len(outputs) == 9
    assert "stage-02.out.md" not in outputs

    counting = tmp_path / "counting-agent.sh"
    counting.write_text(
        "#!/bin/sh\n"
        f"echo x >> {counter}\n"
        "echo \"$2\" | grep -o -- '--- Stage [0-9][0-9] output ---' | wc -l\n"
    )
    counting.chmod(0o755)
    second = subprocess.run(base + ["--agent-bin", str(counting), "--resume"], capture_output=True, text=True, timeout=120)
    assert second.returncode == 0, second.stderr
    assert second.stdout.count("SKIP") == 9
    outputs = sorted(p.name for p in out_dir.iterdir())
    assert len(outputs) == 10
    # only stage 02 ran on resume and it chained the on-disk stage 01 output
    # (first run called the agent 10 times, including the rejected stage 02)
    calls_after_resume = counter.read_text(encoding="utf-8").count("x")
    assert calls_after_resume == 11
    assert (out_dir / "stage-02.out.md").read_text(encoding="utf-8").strip() == "1"


def test_resume_requires_all(tmp_path, fake_agent):
    mod = load_runner()
    ctx = tmp_path / "ctx.yaml"
    ctx.write_text("project: Test\nsubsystem: Chain\n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable, str(RUN_REVIEW),
            "--stage", "01", "--resume", "--context", str(ctx),
            "--agent-bin", str(fake_agent), "--output-dir", str(tmp_path / "r"),
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode != 0
    assert "--resume requires --all" in result.stderr


def test_all_shared_session(tmp_path):
    """--all --session-mode shared: the first stage starts the session (with
    --name), the remaining stages continue it with the session id only."""
    args_log = tmp_path / "args.log"
    agent = tmp_path / "record-agent.sh"
    agent.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" >> {args_log}\necho OK\n")
    agent.chmod(0o755)
    ctx = tmp_path / "ctx.yaml"
    ctx.write_text("project: Test\nsubsystem: Chain\n", encoding="utf-8")
    out_dir = tmp_path / "reviews"
    result = subprocess.run(
        [
            sys.executable, str(RUN_REVIEW),
            "--all", "--context", str(ctx),
            "--agent-bin", str(agent),
            "--output-dir", str(out_dir),
            "--repo", str(tmp_path),
            "--session-mode", "shared",
            "--session-name", "rev1",
        ],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    lines = args_log.read_text(encoding="utf-8").splitlines()
    assert lines.count("rev1") == 11  # 1 display name + 10 session-id uses
    assert lines.count("--name") == 1  # only the first stage names the session
    assert len(list(out_dir.iterdir())) == 10


def test_shared_session_requires_all(tmp_path, fake_agent):
    mod = load_runner()
    ctx = tmp_path / "ctx.yaml"
    ctx.write_text("project: Test\nsubsystem: Chain\n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable, str(RUN_REVIEW),
            "--stage", "01", "--session-mode", "shared", "--context", str(ctx),
            "--agent-bin", str(fake_agent), "--output-dir", str(tmp_path / "r"),
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode != 0
    assert "--session-mode shared requires --all" in result.stderr


def test_run_stage_validation_failure_not_saved(tmp_path, fake_agent):
    """A failing --validate-cmd rejects the stage and leaves no file; a
    passing gate saves the output."""
    mod = load_runner()
    args = _Args(agent_bin=str(fake_agent), output_dir=str(tmp_path), repo=str(tmp_path), validate_cmd="exit 3")
    rc = mod.run_stage("00", "PROMPT BODY", args)
    assert rc == 1
    assert not (tmp_path / "stage-00.out.md").exists()

    args = _Args(agent_bin=str(fake_agent), output_dir=str(tmp_path), repo=str(tmp_path), validate_cmd="true")
    rc = mod.run_stage("00", "PROMPT BODY", args)
    assert rc == 0
    assert (tmp_path / "stage-00.out.md").exists()


def test_all_rejected_stage_is_skipped_in_chaining(tmp_path):
    """A stage whose agent output carries a provider failure signature must
    leave no md file and must not feed later stages. Stage 02 is rejected, so
    stage 04 chains only stage 01."""
    agent = tmp_path / "mixed-agent.sh"
    agent.write_text(
        "#!/bin/sh\n"
        "case \"$2\" in\n"
        "  *\"# Stage 02\"*) echo \"Error: rate_limit_error\" ; exit 0 ;;\n"
        "  *) echo \"$2\" | grep -o -- '--- Stage [0-9][0-9] output ---' | wc -l ;;\n"
        "esac\n"
    )
    agent.chmod(0o755)
    ctx = tmp_path / "ctx.yaml"
    ctx.write_text("project: Test\nsubsystem: Chain\n", encoding="utf-8")
    out_dir = tmp_path / "reviews"
    result = subprocess.run(
        [
            sys.executable, str(RUN_REVIEW),
            "--all", "--context", str(ctx),
            "--agent-bin", str(agent),
            "--output-dir", str(out_dir),
            "--repo", str(tmp_path),
        ],
        capture_output=True, text=True, timeout=120,
    )
    # stage 02 fails -> overall non-zero, but later stages still run
    assert result.returncode != 0
    outputs = sorted(p.name for p in out_dir.iterdir())
    assert len(outputs) == 9
    assert "stage-02.out.md" not in outputs
    assert (out_dir / "stage-00.out.md").read_text(encoding="utf-8").strip() == "0"
    assert (out_dir / "stage-03.out.md").read_text(encoding="utf-8").strip() == "1"
    assert (out_dir / "stage-04.out.md").read_text(encoding="utf-8").strip() == "2"
