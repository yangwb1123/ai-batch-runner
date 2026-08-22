"""Headless, read-only Codex review runner with structured findings."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, TextIO


AGENT_TEXT_MAX_BYTES = 256 * 1024
EVENT_LINE_MAX_BYTES = 1024 * 1024
STDERR_MAX_BYTES = 64 * 1024
SUMMARY_MAX_BYTES = 4096
INVALID_MAX_ITEMS = 256
FIELD_MAX_BYTES = 8192
SEVERITIES = ("critical", "high", "medium", "low", "info")
FINDING_FIELDS = ("severity", "file", "line", "problem", "evidence", "fix_hint")
_JSON_FENCE_RE = re.compile(r"```json[ \t]*\r?\n(?P<body>.*?)```", re.I | re.S)


@dataclass
class RunOutcome:
    codex_exit: int
    elapsed_ms: int
    timed_out: bool = False
    stderr: str = ""


@dataclass
class ReviewCapture:
    """Bounded projection of the Codex JSONL event stream."""

    item_counts: Counter[str] = field(default_factory=Counter)
    messages: list[str] = field(default_factory=list)
    invalid_events: list[str] = field(default_factory=list)
    flagged_writes: bool = False
    turn_started: bool = False
    turn_completed: bool = False
    _message_bytes: int = 0

    def consume_jsonl(self, raw: str) -> None:
        raw_bytes = raw.encode("utf-8", "replace")
        if len(raw_bytes) > EVENT_LINE_MAX_BYTES:
            self._invalid_event("JSONL event exceeded 1 MiB")
            return
        try:
            value = json.loads(raw)
        except (TypeError, ValueError, RecursionError):
            self._invalid_event("malformed JSONL event")
            return
        if not isinstance(value, Mapping):
            self._invalid_event("JSONL event was not an object")
            return
        self._consume_event(value)

    def _consume_event(self, value: Mapping[str, Any]) -> None:
        event_type = str(value.get("type", "")).replace("/", ".")
        if event_type == "turn.started":
            self.turn_started = True
        elif event_type == "turn.completed":
            self.turn_completed = True
        if not event_type.startswith("item."):
            return
        item = value.get("item")
        if not isinstance(item, Mapping):
            self._invalid_event(f"{event_type} omitted item object")
            return
        item_type = _item_type(item.get("type"), self.item_counts)
        self.item_counts[item_type] += 1
        if item_type == "agent_message":
            self._append_message(item.get("text", ""))
        elif item_type == "file_change":
            self.flagged_writes = True

    def _append_message(self, value: Any) -> None:
        text = str(value if value is not None else "")
        remaining = AGENT_TEXT_MAX_BYTES - self._message_bytes
        if remaining <= 0:
            self._invalid_event("agent_message text exceeded 256 KiB")
            return
        encoded = text.encode("utf-8", "replace")
        kept = encoded[:remaining].decode("utf-8", "ignore")
        self.messages.append(kept)
        self._message_bytes += len(kept.encode("utf-8"))
        if len(encoded) > remaining:
            self._invalid_event("agent_message text exceeded 256 KiB")

    def _invalid_event(self, message: str) -> None:
        if len(self.invalid_events) < INVALID_MAX_ITEMS:
            self.invalid_events.append(message)


def _item_type(value: Any, counts: Counter[str]) -> str:
    text = str(value or "unknown").strip()
    if not text or len(text.encode("utf-8", "replace")) > 128:
        return "unknown"
    if text not in counts and len(counts) >= 64:
        return "other"
    return text


def _positive_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a number") from exc
    if timeout <= 0:
        raise argparse.ArgumentTypeError("timeout must be greater than zero")
    return timeout


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pi-batch review",
        description="Run a headless, read-only Codex review and emit structured findings.",
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--uncommitted", action="store_true")
    scope.add_argument("--base", metavar="BRANCH")
    scope.add_argument("--commit", metavar="SHA")
    scope.add_argument("--custom", metavar="TEXT", help="custom review instructions")
    parser.add_argument("--json", action="store_true", help="write and print JSON report")
    parser.add_argument("--output", metavar="PATH")
    parser.add_argument("--timeout", type=_positive_timeout, default=600.0)
    parser.add_argument("--cwd", default=os.getcwd())
    return parser


def _scope_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> tuple[str, list[str]]:
    if args.base is not None:
        if not args.base.strip():
            parser.error("--base cannot be empty")
        return f"base:{args.base}", ["--base", args.base]
    if args.commit is not None:
        if not args.commit.strip():
            parser.error("--commit cannot be empty")
        return f"commit:{args.commit}", ["--commit", args.commit]
    if args.custom is not None:
        if not args.custom.strip():
            parser.error("--custom cannot be empty")
        return "custom", [args.custom]
    if args.uncommitted:
        return "uncommitted", ["--uncommitted"]
    return "default", []


def _paths(args: argparse.Namespace, parser: argparse.ArgumentParser) -> tuple[Path, Path]:
    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.is_dir():
        parser.error(f"--cwd is not a directory: {cwd}")
    if args.output:
        candidate = Path(args.output).expanduser()
        output = (cwd / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        suffix = ".json" if args.json else ".md"
        output = (cwd / "docs" / "reviews" / f"review-{stamp}{suffix}").resolve()
    try:
        output.relative_to(cwd)
    except ValueError:
        parser.error("--output must resolve inside --cwd")
    return cwd, output


def _spawn_process(argv: Sequence[str], cwd: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        list(argv), cwd=cwd, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )


def _read_stdout(stream: TextIO, capture: ReviewCapture) -> None:
    try:
        while chunk := stream.readline(EVENT_LINE_MAX_BYTES + 1):
            if len(chunk.encode("utf-8", "replace")) > EVENT_LINE_MAX_BYTES:
                capture._invalid_event("JSONL event exceeded 1 MiB")
                _drain_line(stream, chunk)
                continue
            capture.consume_jsonl(chunk)
    except (OSError, ValueError) as exc:
        capture._invalid_event(f"stdout read failed: {_bounded_text(exc, 512)}")
    finally:
        stream.close()


def _drain_line(stream: TextIO, first: str) -> None:
    if first.endswith("\n"):
        return
    while chunk := stream.readline(8192):
        if chunk.endswith("\n"):
            return


def _read_stderr(stream: TextIO, chunks: list[str]) -> None:
    used = 0
    try:
        while chunk := stream.readline(8192):
            encoded = chunk.encode("utf-8", "replace")
            remaining = STDERR_MAX_BYTES - used
            if remaining > 0:
                kept = encoded[:remaining].decode("utf-8", "ignore")
                chunks.append(kept)
                used += len(kept.encode("utf-8"))
    except (OSError, ValueError):
        pass
    finally:
        stream.close()


def _kill_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=1)
    except (subprocess.TimeoutExpired, ChildProcessError):
        pass


def _run_codex(argv: Sequence[str], cwd: str, timeout: float,
               capture: ReviewCapture) -> RunOutcome:
    started = time.monotonic()
    stderr_chunks: list[str] = []
    try:
        process = _spawn_process(argv, cwd)
    except OSError as exc:
        return RunOutcome(-1, _elapsed_ms(started), stderr=_bounded_text(exc))
    stdout_thread = threading.Thread(
        target=_read_stdout, args=(process.stdout, capture), daemon=True)
    stderr_thread = threading.Thread(
        target=_read_stderr, args=(process.stderr, stderr_chunks), daemon=True)
    stdout_thread.start(); stderr_thread.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(process)
    stdout_thread.join(timeout=1); stderr_thread.join(timeout=1)
    exit_code = -1 if timed_out else int(process.returncode or 0)
    return RunOutcome(exit_code, _elapsed_ms(started), timed_out,
                      "".join(stderr_chunks))


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _bounded_text(value: Any, maximum: int = SUMMARY_MAX_BYTES) -> str:
    encoded = str(value if value is not None else "").encode("utf-8", "replace")
    return encoded[:maximum].decode("utf-8", "ignore")


def extract_findings(messages: Sequence[str]) -> tuple[list[dict[str, str]], list[str]]:
    findings: list[dict[str, str]] = []
    invalid: list[str] = []
    for message_index, text in enumerate(messages, 1):
        spans = []
        for block_index, match in enumerate(_JSON_FENCE_RE.finditer(text), 1):
            spans.append(match.span())
            _consume_json_array(match.group("body"), findings, invalid,
                                f"message {message_index} fence {block_index}")
        bare = _without_spans(text, spans)
        _consume_bare_arrays(bare, findings, invalid, f"message {message_index} bare")
    return findings, invalid[:INVALID_MAX_ITEMS]


def _without_spans(text: str, spans: Sequence[tuple[int, int]]) -> str:
    if not spans:
        return text
    parts = list(text)
    for start, end in spans:
        parts[start:end] = " " * (end - start)
    return "".join(parts)


def _consume_bare_arrays(text: str, findings: list[dict[str, str]],
                         invalid: list[str], source: str) -> None:
    decoder = json.JSONDecoder()
    cursor = 0
    while cursor < len(text):
        start = text.find("[", cursor)
        if start < 0:
            return
        try:
            value, size = decoder.raw_decode(text[start:])
        except (ValueError, RecursionError):
            cursor = start + 1
            continue
        cursor = start + max(size, 1)
        _consume_value(value, findings, invalid, source)


def _consume_json_array(raw: str, findings: list[dict[str, str]],
                        invalid: list[str], source: str) -> None:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        _add_invalid(invalid, f"{source}: malformed JSON")
        return
    _consume_value(value, findings, invalid, source)


def _consume_value(value: Any, findings: list[dict[str, str]],
                   invalid: list[str], source: str) -> None:
    if not isinstance(value, list):
        _add_invalid(invalid, f"{source}: expected a JSON array")
        return
    for index, item in enumerate(value):
        normalized = _normalize_finding(item, invalid, f"{source}[{index}]")
        if normalized is not None:
            findings.append(normalized)


def _normalize_finding(value: Any, invalid: list[str], source: str) -> Optional[dict[str, str]]:
    if not isinstance(value, Mapping):
        _add_invalid(invalid, f"{source}: finding is not an object")
        return None
    severity = _bounded_text(value.get("severity", ""), 128).strip().lower()
    file_name = _bounded_text(value.get("file", ""), 4096).strip()
    missing = [name for name, item in (("severity", severity), ("file", file_name)) if not item]
    if missing:
        _add_invalid(invalid, f"{source}: missing {', '.join(missing)}")
        return None
    if severity not in SEVERITIES:
        _add_invalid(invalid, f"{source}: unknown severity {severity!r}; normalized to medium")
        severity = "medium"
    result = {name: _bounded_text(value.get(name, ""), FIELD_MAX_BYTES)
              for name in FINDING_FIELDS}
    result["severity"] = severity
    result["file"] = file_name
    return result


def _add_invalid(invalid: list[str], message: str) -> None:
    if len(invalid) < INVALID_MAX_ITEMS:
        invalid.append(_bounded_text(message, 1024))


def _summary(capture: ReviewCapture, outcome: RunOutcome,
             findings: Sequence[Mapping[str, str]], invalid: Sequence[str]) -> str:
    counts = Counter(item["severity"] for item in findings)
    parts = [f"{len(findings)} structured finding(s)",
             ", ".join(f"{name}={counts[name]}" for name in SEVERITIES),
             f"invalid={len(invalid) + len(capture.invalid_events)}",
             f"turn_started={str(capture.turn_started).lower()}",
             f"turn_completed={str(capture.turn_completed).lower()}"]
    if outcome.timed_out:
        parts.append("Codex review timed out")
    elif outcome.codex_exit != 0:
        parts.append(f"Codex exited {outcome.codex_exit}")
    if not findings:
        parts.append("no structured findings extracted")
        if capture.messages:
            parts.append("agent summary: " + _bounded_text(capture.messages[-1], 1024))
    if outcome.stderr.strip() and (outcome.timed_out or outcome.codex_exit != 0):
        parts.append("stderr: " + _bounded_text(outcome.stderr.strip(), 2048))
    if capture.flagged_writes:
        parts.append("read-only contract violation: file_change observed")
    return _bounded_text("; ".join(parts))


def _payload(scope: str, output: Path, capture: ReviewCapture,
             outcome: RunOutcome) -> dict[str, Any]:
    findings, invalid = extract_findings(capture.messages)
    all_invalid = (invalid + capture.invalid_events)[:INVALID_MAX_ITEMS]
    return {
        "scope": scope,
        "codex_exit": outcome.codex_exit,
        "elapsed_ms": outcome.elapsed_ms,
        "findings": findings,
        "invalid_findings": all_invalid,
        "item_counts": dict(sorted(capture.item_counts.items())),
        "flagged_writes": capture.flagged_writes,
        "summary": _summary(capture, outcome, findings, invalid),
        "report_path": str(output),
    }


def _markdown(payload: Mapping[str, Any], timed_out: bool) -> str:
    findings = payload["findings"]
    counts = Counter(item["severity"] for item in findings)
    lines = ["# Codex review", "", f"- Scope: `{payload['scope']}`",
             f"- Duration: {payload['elapsed_ms']} ms",
             f"- Codex exit: {payload['codex_exit']}",
             f"- Timed out: {'yes' if timed_out else 'no'}",
             "- Findings: " + ", ".join(f"{name}={counts[name]}" for name in SEVERITIES), ""]
    if payload["flagged_writes"]:
        lines += ["> WARNING: Codex emitted a `file_change` event; the read-only contract was violated.", ""]
    if not findings:
        lines += ["No structured findings were extracted; this run fails closed.", ""]
    for severity in SEVERITIES:
        grouped = [item for item in findings if item["severity"] == severity]
        if grouped:
            lines += _finding_table(severity, grouped)
    invalid = payload["invalid_findings"]
    if invalid:
        lines += ["## Invalid findings/events", ""]
        lines += [f"- {_md_cell(item)}" for item in invalid]
        lines.append("")
    lines += ["## Summary", "", str(payload["summary"]), ""]
    return "\n".join(lines)


def _finding_table(severity: str, findings: Sequence[Mapping[str, str]]) -> list[str]:
    lines = [f"## {severity.upper()} ({len(findings)})", "",
             "| File:line | Problem | Evidence | Fix hint |",
             "|---|---|---|---|"]
    for finding in findings:
        location = finding["file"] + (f":{finding['line']}" if finding["line"] else "")
        lines.append("| " + " | ".join(_md_cell(value) for value in (
            location, finding["problem"], finding["evidence"], finding["fix_hint"])) + " |")
    lines.append("")
    return lines


def _md_cell(value: Any) -> str:
    return _bounded_text(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _write_report(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _exit_code(payload: Mapping[str, Any], outcome: RunOutcome) -> int:
    if payload["flagged_writes"]:
        return 3
    if outcome.timed_out or outcome.codex_exit != 0 or not payload["findings"]:
        return 2
    if any(item["severity"] in ("critical", "high") for item in payload["findings"]):
        return 1
    return 0


Runner = Callable[[Sequence[str], str, float, ReviewCapture], RunOutcome]


def review_main(argv: Optional[list[str]] = None, *, runner: Optional[Runner] = None) -> int:
    """Run Codex review; ``runner`` is the local-test injection boundary."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    scope, scope_argv = _scope_args(args, parser)
    cwd, output = _paths(args, parser)
    command = ["codex", "exec", "review", "--json", *scope_argv]
    capture = ReviewCapture()
    execute = runner or _run_codex
    try:
        outcome = execute(command, str(cwd), args.timeout, capture)
    except Exception as exc:
        outcome = RunOutcome(-1, 0, stderr=f"review runner failed: {_bounded_text(exc)}")
    payload = _payload(scope, output, capture, outcome)
    if args.json:
        report = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        _write_report(output, report)
        print(report, end="")
    else:
        _write_report(output, _markdown(payload, outcome.timed_out))
        print(f"review report: {output}")
        print(payload["summary"])
    return _exit_code(payload, outcome)
