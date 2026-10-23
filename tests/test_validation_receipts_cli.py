"""Read-only inspection of bounded validator invocation receipts."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from pbatch import config, validation_receipts as receipts


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(config, "_DATA_DIR_OVERRIDE", str(runtime))
    return runtime / "validation" / "receipts.jsonl"


def _record(index):
    return {"schema_version": 1, "kind": "validator_invocation",
            "command_hash": f"command-{index}", "scope": "task",
            "ok": None, "exit_code": None}


def _write_ledger(path, lines):
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_bytes(b"\n".join(lines) + b"\n")
    path.chmod(0o600)


def test_reader_skips_malformed_lines_without_consuming_receipt_limit(ledger):
    expected = [_record(1), _record(2)]
    _write_ledger(ledger, [json.dumps(_record(0)).encode(), b"{broken",
                           json.dumps(expected[0]).encode(), b"\xff", b"[]",
                           b'{"schema_version":1,"kind":"pipeline_stage"}',
                           json.dumps(expected[1]).encode(), b"null", b""])
    before = ledger.read_bytes()

    assert receipts.read_receipts(limit=2) == expected
    assert ledger.read_bytes() == before


def test_reader_skips_deeply_nested_json_and_keeps_valid_records(ledger):
    expected = [_record(1), _record(2)]
    _write_ledger(ledger, [json.dumps(expected[0]).encode(), b"[" * 2000,
                           json.dumps(expected[1]).encode()])

    assert receipts.read_receipts() == expected


def test_reader_returns_newest_last_and_clamps_record_budget(ledger, monkeypatch):
    records = [_record(index) for index in range(7)]
    _write_ledger(ledger, [json.dumps(record).encode() for record in records])
    monkeypatch.setattr(receipts.storage, "READ_MAX_RECORDS", 3)

    assert receipts.read_receipts(limit=2) == records[-2:]
    assert receipts.read_receipts(limit=10_000) == records[-3:]


def test_reader_bounds_tail_bytes_and_skips_oversized_lines(ledger, monkeypatch):
    expected = _record(2)
    last = json.dumps(expected).encode()
    oversized = json.dumps({**_record(1), "padding": "x" * 1024}).encode()
    _write_ledger(ledger, [json.dumps(_record(0)).encode(), oversized, last])
    monkeypatch.setattr(receipts.storage, "RECEIPTS_MAX_BYTES", len(last) + 300)
    monkeypatch.setattr(receipts.storage, "RECEIPTS_MAX_LINE_BYTES", len(last))
    tail_bytes = receipts.storage._tail_bytes
    reads = []

    def bounded_tail(path, maximum):
        reads.append(maximum)
        return tail_bytes(path, maximum)

    monkeypatch.setattr(receipts.storage, "_tail_bytes", bounded_tail)
    assert receipts.read_receipts(limit=200) == [expected]
    assert reads == [len(last) + 300]


@pytest.mark.parametrize("limit", ["0", "-1", "201", "99999999999999999999"])
def test_cli_rejects_out_of_range_limits_without_reading(ledger, capsys, limit):
    from pbatch.validation_receipt_cli import main

    assert main(["receipts", "--limit", limit]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("validation receipts:")
    assert "1..200" in captured.err
    assert not ledger.parent.exists()


@pytest.mark.parametrize("limit", ["invalid", "1.5"])
def test_cli_rejects_noninteger_limits(ledger, capsys, limit):
    from pbatch.validation_receipt_cli import main

    with pytest.raises(SystemExit) as error:
        main(["receipts", "--limit", limit])
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "validation receipts:" in captured.err
    assert not ledger.parent.exists()


@pytest.mark.parametrize("limit", [1, 50, 200])
def test_cli_json_shape_and_valid_limit_boundaries(ledger, capsys, limit):
    from pbatch.validation_receipt_cli import main

    records = [_record(index) for index in range(205)]
    _write_ledger(ledger, [json.dumps(record).encode() for record in records])
    before = ledger.read_bytes()
    assert main(["receipts", "--limit", str(limit), "--json"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {"receipts": records[-limit:]}
    assert ledger.read_bytes() == before


def test_cli_default_limit_and_text_output(ledger, capsys):
    from pbatch.validation_receipt_cli import main

    records = [_record(index) for index in range(60)]
    _write_ledger(ledger, [json.dumps(record).encode() for record in records])
    assert main(["receipts"]) == 0
    captured = capsys.readouterr()
    header, *lines = captured.out.splitlines()
    assert header == "validation receipts: 50 record(s) (newest last)"
    assert [json.loads(line) for line in lines] == records[-50:]
    assert captured.err == ""


def test_cli_missing_ledger_does_not_create_runtime(ledger, capsys):
    from pbatch.validation_receipt_cli import main

    assert main(["receipts", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"receipts": []}
    assert not ledger.parents[1].exists()


def test_cli_dispatch_and_process_exit_codes(ledger):
    entry = Path(__file__).resolve().parents[1] / "pi-batch.py"
    env = {**os.environ, "PBATCH_DATA_DIR": str(ledger.parents[1])}
    result = subprocess.run([sys.executable, str(entry), "validation", "receipts",
                             "--json"], env=env, capture_output=True, text=True,
                            timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"receipts": []}
    result = subprocess.run([sys.executable, str(entry), "validation", "receipts",
                             "--limit", "201"], env=env, capture_output=True,
                            text=True, timeout=10)
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("validation receipts:")
    assert not ledger.parents[1].exists()
