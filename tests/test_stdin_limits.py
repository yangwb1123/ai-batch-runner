"""Input budgets apply before logging/locks and before whitespace removal."""

import io
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from pbatch import cli, config
from pbatch.text_io import read_stream_bounded, read_text_bounded


@pytest.mark.parametrize("factory", [io.BytesIO, lambda raw: io.StringIO(raw.decode())])
def test_stream_budget_counts_utf8_bytes(factory):
    raw = "中文".encode()
    assert read_stream_bounded(factory(raw), 6) == "中文"
    with pytest.raises(ValueError, match="exceeds 5 bytes"):
        read_stream_bounded(factory(raw), 5)


def test_infinite_stream_stops_after_one_overflow_byte():
    class Producer:
        total = 0

        def read(self, size):
            assert 0 < size <= 9
            chunk = b"x" * min(size, 3)
            self.total += len(chunk)
            return chunk

    producer = Producer()
    with pytest.raises(ValueError, match="exceeds 8 bytes"):
        read_stream_bounded(producer, 8)
    assert producer.total == 9


def test_stdin_uses_strict_raw_utf8_instead_of_lossy_text_decoder():
    stream = io.TextIOWrapper(io.BytesIO(b"private-text-\xff"), errors="replace")
    with pytest.raises(ValueError, match="not valid UTF-8") as caught:
        read_stream_bounded(stream, 64)
    assert "private-text" not in str(caught.value)


@pytest.mark.parametrize("payload", [b" " * 33, b"secret-\xff", "中" * 11])
def test_cli_rejects_bad_stdin_before_creating_log(tmp_path, payload):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "pi-batch.yaml").write_text("input:\n  max_bytes: 32\n")
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("PBATCH_")}
    environment.update(PBATCH_SCRIPT_DIR=str(config_dir), PBATCH_HOME=str(tmp_path / "home"),
                       PBATCH_RUNTIME_HOME=str(tmp_path / "runtime"))
    log = tmp_path / "operation.log"
    result = subprocess.run(
        [sys.executable, "-m", "pbatch", "--dry-run", "--log-file", str(log)],
        input=payload.encode() if isinstance(payload, str) else payload,
        cwd=Path(__file__).resolve().parents[1], env=environment,
        capture_output=True, timeout=10)
    assert result.returncode == 2, result.stderr
    assert b"Input rejected:" in result.stderr
    assert b"Traceback" not in result.stderr and b"secret-" not in result.stderr
    assert not log.exists()
    assert not list((tmp_path / "runtime").rglob("*.lock"))


def test_empty_buffered_stdin_is_never_consumed_again(monkeypatch):
    class ForbiddenReader:
        def read(self, size=-1):
            raise AssertionError("buffered stdin was consumed again")

    monkeypatch.setattr(sys, "stdin", ForbiddenReader())
    args = SimpleNamespace(from_dir="", prompt="", source="", stdin_prompt="")
    with pytest.raises(SystemExit) as caught:
        cli._load_batch_tasks(args, None)
    assert caught.value.code == 1


def test_direct_batch_loading_has_the_same_stdin_budget(monkeypatch):
    monkeypatch.setattr(config, "INPUT_MAX_BYTES", 4)
    monkeypatch.setattr(sys, "stdin", io.StringIO("abcde"))
    args = SimpleNamespace(from_dir="", prompt="", source="")
    with pytest.raises(SystemExit) as caught:
        cli._load_batch_tasks(args, None)
    assert caught.value.code == 2


def test_lock_output_prescan_does_not_open_oversized_source(tmp_path, monkeypatch):
    source = tmp_path / "tasks.yaml"
    source.write_text("tasks:\n  - prompt: sample\n    output: result.md\n")
    monkeypatch.setattr(config, "INPUT_MAX_BYTES", 8)
    args = SimpleNamespace(output="", prompt="", source=str(source))
    original = os.open
    opened = []

    def guarded_open(path, *args, **kwargs):
        if Path(path) == source:
            opened.append(path)
            raise OSError("oversized source was opened")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded_open)
    assert cli._planned_outputs(args) == []
    assert opened == [], "oversized file must be rejected before reading"


@pytest.mark.parametrize("payload", ["tasks: 42", "stages: true",
                                     "tasks: [{prompt: a, output: 42}]"])
def test_lock_output_prescan_leaves_invalid_schema_to_task_validator(tmp_path, payload):
    source = tmp_path / "tasks.yaml"
    source.write_text(payload)
    args = SimpleNamespace(output="", prompt="", source=str(source))
    assert cli._planned_outputs(args) == []


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX named pipes")
@pytest.mark.parametrize("replace_after_stat", [False, True])
def test_prompt_fifo_is_rejected_without_waiting_for_writer(tmp_path, replace_after_stat):
    path = tmp_path / "prompt.txt"
    path.write_text("small prompt")
    code = """
import os, sys
from pathlib import Path
from pbatch.text_io import read_text_bounded
path = Path(sys.argv[1])
if sys.argv[2] == 'True':
    original = os.open
    def swap(name, flags, *args, **kwargs):
        path.unlink()
        os.mkfifo(path)
        return original(name, flags, *args, **kwargs)
    os.open = swap
else:
    path.unlink()
    os.mkfifo(path)
try:
    read_text_bounded(path, 32, 'prompt')
except ValueError as error:
    assert 'regular file' in str(error), str(error)
else:
    raise AssertionError('FIFO was accepted as a prompt')
"""
    result = subprocess.run([sys.executable, "-c", code, str(path), str(replace_after_stat)],
                            capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr


def test_regular_prompt_symlink_remains_supported(tmp_path):
    source = tmp_path / "prompt.txt"
    source.write_text("中文 prompt", encoding="utf-8")
    link = tmp_path / "linked.txt"
    link.symlink_to(source)
    assert read_text_bounded(link, 32) == "中文 prompt"
