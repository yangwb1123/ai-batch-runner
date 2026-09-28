#!/usr/bin/env python3
"""Recheck saved provider turn evidence without starting a provider or model."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import tempfile

from provider_smoke_support import ProviderJournal, validate_cancel, validate_success, write_report
from tui_soak_support import SoakFailure, executable_evidence


def read_json(path: Path) -> dict:
    with path.open("rb") as handle:
        raw = handle.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise SoakFailure("saved report exceeded its size limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise SoakFailure("saved report is not an object")
    return value


def fingerprint(path: Path) -> dict:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for data in iter(lambda: handle.read(65536), b""):
            digest.update(data)
            size += len(data)
    return {"path": str(path), "sha256": digest.hexdigest(), "bytes": size}


def read_commands(path: Path) -> tuple[list[str], str]:
    with path.open("rb") as handle:
        raw = handle.read(16385)
    if len(raw) > 16384:
        raise SoakFailure("saved commands exceeded their size limit")
    commands = [json.loads(line)["command"] for line in raw.splitlines()]
    if len(commands) != 7 or commands[3:5] != ["b'\\x03'", "/new"] or commands[-1] != "b'\\x11'":
        raise SoakFailure("saved commands do not follow the four-turn acceptance sequence")
    matched = re.search(r"\bPBATCH_[A-F0-9]{16}\b", commands[0])
    if not matched or "preceding turn" not in commands[1] or "1000" not in commands[2]:
        raise SoakFailure("saved prompts do not contain the acceptance marker and controls")
    token = matched.group()
    if commands[0].count(token) != 2 or "RECOVERED_" + token not in commands[5]:
        raise SoakFailure("saved prompts disagree about the acceptance marker")
    return commands, token


def load_journals(root: Path, journal: ProviderJournal) -> list[dict]:
    files, total = [], 0
    for path in sorted((root / "runtime").rglob("events.jsonl")):
        if len(files) >= 16:
            raise SoakFailure("saved evidence exceeded 16 journal files")
        before = fingerprint(path)
        total += before["bytes"]
        if total > 32 * 1024 * 1024:
            raise SoakFailure("saved evidence exceeded 32 MiB")
        with path.open("rb") as handle:
            while raw := handle.readline(1024 * 1024 + 1):
                if len(raw) > 1024 * 1024 or not raw.endswith(b"\n"):
                    raise SoakFailure("saved journal contains an oversized or partial record")
                journal._accept(json.loads(raw))
        if fingerprint(path) != before:
            raise SoakFailure("saved journal changed during revalidation")
        files.append(before)
    if not files:
        raise SoakFailure("no saved journals were found")
    return files


def check_original(report: dict) -> list[dict]:
    if (report.get("status") != "passed" or report.get("exit_code") != 0
            or report.get("model_prompts_sent") != 4 or report.get("forced_cleanup_pids") != []
            or report.get("owned_processes_alive_after_exit") != []):
        raise SoakFailure("original live run did not pass its four-turn/normal-exit checks")
    turns = report.get("turns", [])
    if [turn.get("label") for turn in turns] != ["exact_reply", "continuation", "cancel", "recovery"]:
        raise SoakFailure("original turn labels do not match the acceptance plan")
    return turns


def check_turns(original: list[dict], journal: ProviderJournal, token: str) -> list[dict]:
    if len(journal.turns) != 4 or {item["turn_id"] for item in original} != set(journal.turns):
        raise SoakFailure("saved journals do not match the four original turn identities")
    result = []
    for recorded in original:
        turn = journal.turns[recorded["turn_id"]]
        if recorded["session_id"] != turn["session_id"] or recorded["terminal"] != turn["terminal"]:
            raise SoakFailure("original terminal claims disagree with raw event evidence")
        label = recorded["label"]
        if label == "cancel":
            turn["cancel_requested"] = recorded.get("cancel_requested") is True
            validate_cancel(turn)
        else:
            validate_success(turn, "RECOVERED_" + token if label == "recovery" else token)
        result.append({**{key: value for key, value in turn.items() if key != "messages"}, "label": label})
    if result[0]["session_id"] != result[1]["session_id"] or result[0]["session_id"] == result[3]["session_id"]:
        raise SoakFailure("continuation or fresh-session recovery has an incorrect session identity")
    return result


def revalidate(root: Path) -> dict:
    report_path, commands_path = root / "report.json", root / "commands.jsonl"
    sources = [fingerprint(report_path), fingerprint(commands_path)]
    report = read_json(report_path)
    original = check_original(report)
    _, token = read_commands(commands_path)
    executable = executable_evidence([report["executable"]["path"]])
    if executable["sha256"] != report["executable"]["sha256"]:
        raise SoakFailure("saved report's binary hash does not match the existing artifact")
    with tempfile.TemporaryDirectory(prefix="pbatch-provider-audit-") as temporary:
        journal = ProviderJournal(Path(temporary) / "runtime")
        try:
            journals = load_journals(root, journal)
            turns = check_turns(original, journal, token)
            summary = journal.summary()
        finally:
            journal.close()
    if sources != [fingerprint(report_path), fingerprint(commands_path)]:
        raise SoakFailure("saved report or command evidence changed during revalidation")
    tools = [Path(__file__).resolve(), *[Path(__file__).with_name(name).resolve()
             for name in ("provider_smoke_support.py", "tui_soak_support.py", "tui_soak_watch.py")]]
    return {"schema_version": 1, "kind": "saved-provider-evidence-revalidation", "status": "passed",
            "model_calls": 0, "executable": executable, "turns": turns, "journal": summary,
            "sources": sources + journals, "verifiers": [fingerprint(path) for path in tools],
            "limitations": ["Retains original PTY timing and process observations; does not repeat them",
                            "Cannot reconstruct ownership of an older version-check subprocess",
                            "Rechecks raw event scope, actor, ordering, identities and exact replies"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--output", type=Path, required=True, help="new audit report; existing evidence stays unchanged")
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("audit output already exists")
    try:
        result = revalidate(args.evidence.expanduser().resolve())
    except (OSError, ValueError, KeyError, TypeError, SoakFailure) as exc:
        result = {"kind": "saved-provider-evidence-revalidation", "status": "failed",
                  "model_calls": 0, "error": str(exc)}
    write_report(args.output, result)
    print(f"{result['status'].upper()}: {args.output}")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
