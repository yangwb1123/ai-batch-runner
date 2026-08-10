"""Evolution Engineering: turn an incident (RCA) into a rule draft.

The self-evolving loop: production bug / review finding → root-cause
analysis → rule draft (unified Rule Schema) → human approval → merged into
the domain rules.yaml registry → the matcher/gates/roles consume it on the
next task. Drafts are NEVER auto-promoted — an accidental one-off must not
pollute the registry (restraint: human approval gate).

Usage:
    pi-batch learn "订单在并发下创建了两笔重复记录" \
      --category backend --severity error --rule-id DB-IDEMPOTENCY-002
Writes docs/rules/drafts/<rule-id>-<date>.yaml and prints the draft.
"""

from __future__ import annotations

import sys
import re
from datetime import date
from pathlib import Path

from .config import log

DRAFT_DIR = Path("docs/rules/drafts")
CATEGORIES = ("backend", "frontend", "product", "database", "security",
              "testing", "devops")
SEVERITIES = ("error", "warning", "advice")
_RULE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
_FAILURE_MAX_CHARS = 1000
_FAILURE_STATE_RE = re.compile(r"(?i)(fail|reject|error|timeout|halt|cancel)")


def rule_schema_template() -> dict:
    """The unified Rule Schema every draft (and registry rule) follows."""
    return {
        "id": "DRAFT-000",
        "title": "",
        "category": "backend",
        "severity": "error",
        "scope": {"languages": [], "paths": ["src/**"]},
        "trigger": {"operations": []},
        "rationale": "",
        "requirements": [],
        "forbidden": [],
        "exceptions": [{"condition": ""}],
        "verification": {"static": [], "tests": []},
        "examples": {"good": [], "bad": []},
        "owner": "",
        "version": "0.1.0",
        "status": "draft",
    }


def failure_to_draft(failure: str, category: str, severity: str,
                     rule_id: str, owner: str = "") -> dict:
    """Normalize one incident into the unified in-memory Rule Schema."""
    failure = " ".join(str(failure).split())[:_FAILURE_MAX_CHARS]
    if not failure:
        raise ValueError("failure description must not be empty")
    if category not in CATEGORIES:
        raise ValueError(f"unknown rule category: {category}")
    if severity not in SEVERITIES:
        raise ValueError(f"unknown rule severity: {severity}")
    draft = rule_schema_template()
    draft["id"] = rule_id or f"DRAFT-{date.today():%Y%m%d}"
    if not _RULE_ID_RE.fullmatch(draft["id"]):
        raise ValueError(
            "rule id must match [A-Za-z0-9][A-Za-z0-9_-]{0,79}")
    draft["title"] = failure[:80]
    draft["category"] = category
    draft["severity"] = severity
    draft["rationale"] = ("Observed failure: " + failure +
                          ". Root-cause analysis is required before this "
                          "draft can be promoted (edit requirements/forbidden/"
                          "verification with the actual RCA).")
    draft["owner"] = owner
    return draft


def make_draft(failure: str, category: str, severity: str,
               rule_id: str, owner: str = "") -> tuple:
    """Convert an incident through Rule Schema and persist its draft."""
    draft = failure_to_draft(failure, category, severity, rule_id, owner)
    _ensure_draft_dir()
    from .config import yaml
    payload = yaml.safe_dump(draft, allow_unicode=True, sort_keys=False) if yaml else str(draft)
    path = _write_new_draft(draft["id"], payload)
    return path, draft


def _ensure_draft_dir() -> None:
    target = DRAFT_DIR if DRAFT_DIR.is_absolute() else Path.cwd() / DRAFT_DIR
    if any(item.is_symlink() for item in (target, *target.parents)):
        raise ValueError(f"rule draft directory contains a symlink: {DRAFT_DIR}")
    DRAFT_DIR.mkdir(parents=True, exist_ok=True)


def _write_new_draft(rule_id: str, payload: str) -> Path:
    """Create without following or overwriting an existing draft path."""
    stem = f"{rule_id}-{date.today():%Y%m%d}"
    for number in range(1000):
        suffix = "" if number == 0 else f"-{number:03d}"
        path = DRAFT_DIR / f"{stem}{suffix}.yaml"
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(payload)
            return path
        except FileExistsError:
            continue
    raise ValueError(f"too many existing drafts for rule id: {rule_id}")


def memory_hit_to_failure(query: str, hit: dict) -> str:
    """Map public Memory fields to one bounded incident description."""
    if not isinstance(hit, dict):
        raise ValueError("memory search returned an invalid record")
    domains = _memory_domains(hit)
    evidence, status = _failure_evidence(hit)
    if not evidence:
        raise ValueError("memory record has no incident evidence")
    details = [evidence, f"query={query.strip()}" if query.strip() else ""]
    if domains:
        details.append("domains=" + ",".join(str(item) for item in domains[:8]))
    for key in ("stage", "role", "status", "verdict", "observed_verdict", "session_id"):
        value = str(hit.get(key, "")).strip()
        if value and not (key == "status" and value == evidence):
            details.append(f"{key}={value}")
    failure = " | ".join(item for item in details if item)
    return " ".join(failure.split())[:_FAILURE_MAX_CHARS]


def _memory_domains(hit: dict) -> list:
    domains = hit.get("domains", hit.get("domain", []))
    if isinstance(domains, str):
        return [domains]
    return domains if isinstance(domains, list) else []


def _failure_evidence(hit: dict) -> tuple[str, str]:
    status = str(hit.get("status", "")).strip()
    states = [status, str(hit.get("observed_verdict", "")).strip(),
              str(hit.get("verdict", "")).strip()]
    failure_state = next((state for state in states if _FAILURE_STATE_RE.search(state)), "")
    evidence = _first_text(hit, "observed_failure", "error")
    if not evidence:
        reason = _first_text(hit, "reason")
        evidence = reason if failure_state or not any(states) else ""
    return evidence or failure_state, status


def _first_text(item: dict, *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _memory_incident(query: str, hits: list) -> tuple[dict, str]:
    for hit in hits:
        try:
            return hit, memory_hit_to_failure(query, hit)
        except ValueError:
            continue
    raise ValueError("matching memory records contain no failure evidence")


def learn_main(argv: list) -> None:
    """`pi-batch learn "<failure>" --category X --severity Y [--rule-id Z]`."""
    import argparse
    parser = argparse.ArgumentParser(
        prog="pi-batch.py learn",
        description="Turn an incident/RCA into a rule draft (Evolution "
                    "Engineering: failure -> draft -> human approval -> registry).")
    parser.add_argument("failure", nargs="*", default=[],
                        help="observed failure description (or use --from-memory)")
    parser.add_argument("--category", default="backend", choices=CATEGORIES,
                        help="rule category")
    parser.add_argument("--severity", default="error", choices=SEVERITIES,
                        help="rule severity")
    parser.add_argument("--rule-id", default="", help="rule id (default DRAFT-<date>)")
    parser.add_argument("--owner", default="", help="rule owner")
    parser.add_argument("--from-memory", default="",
                        help="use the highest-ranked failure-bearing memory hit "
                             "(Evolution loop: incident -> draft)")
    args = parser.parse_args(argv)
    if not args.failure and not args.from_memory:
        parser.error("Provide a failure description or --from-memory TERM")
    if args.from_memory:
        from .memory import find as memory_find
        hits = memory_find(args.from_memory)
        if not hits:
            log.error("No memory records match %r", args.from_memory)
            sys.exit(1)
        try:
            top, failure = _memory_incident(args.from_memory, hits)
        except ValueError as exc:
            log.error("Cannot learn from memory: %s", exc)
            sys.exit(1)
        log.info("Using memory hit %r as the observed failure", top.get("session_id", ""))
    else:
        failure = " ".join(args.failure)
    try:
        path, draft = make_draft(failure, args.category,
                                 args.severity, args.rule_id, args.owner)
    except ValueError as exc:
        parser.error(str(exc))
    log.info("Rule draft written to %s", path)
    log.info("Review the draft, fill requirements/forbidden/verification "
             "with the RCA, then merge it into the domain rules.yaml "
             "(ui-specs/rules.yaml or backend-specs/rules.yaml).")
    print(path)
