"""Bounded problem ledger for campaign analysis.

A long-running campaign keeps rediscovering the same defect under new wording:
the analysis sees no history, re-proposes it, and a whole pipeline (design,
adversarial review, gate) is spent to reach the same verdict.  The rejected
directions already injected by L6/L10 stop *identical* repeats; this module
adds what a plain status list cannot answer:

* **fixed** — directions whose branch actually merged (evidence: the merge
  record written by the merge transaction), so the analyser stops re-proposing
  solved work;
* **still open** — directions the design gate rejected, paired with the
  gate's own blocking findings, so a genuine defect is carried forward with
  what must be resolved instead of being dropped or silently re-cut.

Everything here is read from artifacts that already exist (campaign state,
merge records, gate verdicts).  A missing artifact yields less context, never
an invented one, and any I/O problem degrades to "no ledger" rather than
failing the analysis.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# Bounded output: the ledger is injected into every analysis prompt.
_MAX_BYTES = 6000
_MAX_TITLE = 110
_MAX_BLOCKING = 200
_MAX_FIXED = 40
_MAX_OPEN = 40
_GATE_FILE = "task-1-design-gate.md"
_MERGE_SUFFIX = "-merge.json"

_STOPWORDS = frozenset((
    "the", "a", "an", "and", "or", "to", "for", "on", "in", "of", "is", "are",
    "be", "so", "that", "this", "it", "with", "without", "into", "from", "by",
    "at", "as", "not", "no", "never", "only", "every", "each", "when", "then",
    "than", "make", "makes", "making", "add", "adds", "bound", "bounds",
    "bounding", "stop", "stops", "stopping", "keep", "keeps", "giving", "give",
    "use", "used", "using", "avoid", "ensure", "ensures", "still", "while",
    "one", "two", "three", "per", "via", "up", "out", "over", "under",
))
_PENDING_MARKERS = re.compile(r"\*\*(Unresolved|Not addressed|Unverified)\*\*")
_VERDICT = re.compile(r"VERDICT:\s*(PASS|FAIL)")


def build_ledger(root: Path, state_path: Path | None,
                 worktree_root: Path | None) -> str:
    """Return the markdown ledger, or "" when there is nothing evidenced."""
    try:
        fixed = _fixed_directions(root, state_path)
        open_problems = _open_problems(root, state_path, worktree_root)
        if not fixed and not open_problems:
            return ""
        return _render(fixed, open_problems)
    except (OSError, ValueError):
        # Context is an optimisation, never a correctness input.
        return ""


def _render(fixed: list[str], open_problems: list[tuple[str, str]]) -> str:
    parts = ["### Problems already FIXED in this repository (do not re-propose)"]
    parts.extend(f"- {title}" for title in fixed[:_MAX_FIXED])
    if not fixed:
        parts.append("- (none recorded)")
    parts.append("")
    parts.append("### Problems that are REAL and still OPEN, with the recorded "
                 "blocking findings")
    parts.append("(attempted before; the design gate rejected them. A reworded "
                 "variant that ignores these items is rejected again.)")
    budget = _MAX_BYTES
    for title, blocking in open_problems[:_MAX_OPEN]:
        line = f"- {title[:_MAX_TITLE]}"
        if blocking:
            line += f"\n  blocking: {blocking[:_MAX_BLOCKING]}"
        if budget - len(line) < 0:
            remaining = len(open_problems[:_MAX_OPEN]) - open_problems.index(
                (title, blocking))
            parts.append(f"- ... and {remaining} more recorded open problems")
            break
        budget -= len(line)
        parts.append(line)
    if not open_problems:
        parts.append("- (none recorded)")
    parts.append("")
    parts.append("Rules: never propose a FIXED item; prefer an OPEN item over "
                 "inventing new scope, and when proposing one, resolve the "
                 "recorded blocking items explicitly or say with evidence why "
                 "the objection is wrong; never re-cut an attempted problem "
                 "into a narrower slice of itself.")
    return "\n".join(parts)


def _state_rows(state_path: Path | None) -> dict[str, dict]:
    """direction_id -> latest state row (bounded scan of the state file)."""
    if state_path is None or not state_path.is_file():
        return {}
    rows: dict[str, dict] = {}
    try:
        with state_path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                did = row.get("direction_id")
                if isinstance(did, str) and did:
                    rows[did] = row
    except OSError:
        return {}
    return rows


def _title_for(did: str, rows: dict[str, dict]) -> str:
    row = rows.get(did) or {}
    title = row.get("direction")
    if isinstance(title, str) and title.strip():
        return title.strip()
    return did.rsplit("-", 1)[0].replace("-", " ").strip()


def _module_for(did: str, rows: dict[str, dict]) -> str:
    module = (rows.get(did) or {}).get("module")
    return module if isinstance(module, str) and not module.startswith("__") else ""


def _merge_dir(root: Path) -> Path:
    from .config import data_dir
    return data_dir(str(root)) / "campaign-merges"


def _fixed_directions(root: Path, state_path: Path | None) -> list[str]:
    """Merged directions: the merge record, not the worktree, is the truth.

    A merged worktree may already have been reaped, so keying on the directory
    would under-report solved work and invite the analyser to redo it.
    """
    merge_dir = _merge_dir(root)
    if not merge_dir.is_dir():
        return []
    rows = _state_rows(state_path)
    fixed = []
    for record in sorted(merge_dir.glob(f"*{_MERGE_SUFFIX}")):
        did = record.name[: -len(_MERGE_SUFFIX)]
        fixed.append(_title_for(did, rows))
    return fixed


def _gate_artifact(worktree_root: Path | None, did: str) -> Path | None:
    if worktree_root is None:
        return None
    best: Path | None = None
    for candidate in sorted(worktree_root.glob(f"*/{did}/**/artifacts/design_gate-*/{_GATE_FILE}")):
        best = candidate
    return best


def _verdict_and_blocking(path: Path | None) -> tuple[str, str]:
    if path is None:
        return "", ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", ""
    verdict = ""
    summary = ""
    for line in reversed(text.splitlines()):
        found = _VERDICT.search(line)
        if found:
            verdict = found.group(1)
            summary = re.sub(r"^VERDICT:\s*[A-Z]+\s*-\s*", "", line).strip()
            break
    blocking = []
    for line in text.splitlines():
        if _PENDING_MARKERS.search(line):
            cells = line.split("|")
            label = cells[1].strip() if len(cells) > 2 else line.strip()
            blocking.append(re.sub(r"[*`]", "", label))
    if not blocking and verdict == "FAIL":
        blocking = [summary]
    return verdict, "; ".join(blocking[:3])


def _open_problems(root: Path, state_path: Path | None,
                   worktree_root: Path | None) -> list[tuple[str, str]]:
    """Gate-rejected directions, deduplicated by problem signature.

    Near-duplicates are the expensive case: each one burns a full pipeline to
    reach the same verdict, so they collapse into one entry carrying the latest
    blocking findings.
    """
    rows = _state_rows(state_path)
    rejected = [did for did, row in rows.items()
                if str(row.get("status") or "") == "GATE_REJECTED"]
    merged = {path.name[: -len(_MERGE_SUFFIX)]
              for path in _merge_dir(root).glob(f"*{_MERGE_SUFFIX}")} \
        if _merge_dir(root).is_dir() else set()
    entries: list[tuple[str, str, str]] = []
    for did in sorted(set(rejected) - merged):
        verdict, blocking = _verdict_and_blocking(_gate_artifact(worktree_root, did))
        if not verdict:
            # Never asserted a verdict: the pipeline did not reach the gate.
            continue
        module = _module_for(did, rows)
        title = _title_for(did, rows)
        label = f"[{module}] {title}" if module else title
        entries.append((label, verdict, blocking))
    return _collapse(entries)


def _collapse(entries: list[tuple[str, str, str]]) -> list[tuple[str, str]]:
    """Group entries whose titles share most content words (wording aside)."""
    clusters: list[dict] = []
    for label, verdict, blocking in entries:
        words = _signature(label)
        for cluster in clusters:
            if _similar(words, cluster["words"]):
                cluster["members"].append((label, verdict, blocking))
                break
        else:
            clusters.append({"words": words, "members": [(label, verdict, blocking)]})
    collapsed = []
    for cluster in clusters:
        label, verdict, blocking = cluster["members"][0]
        extra = len(cluster["members"]) - 1
        title = f"{label} (attempts={len(cluster['members'])}, verdict={verdict})"
        collapsed.append((title, blocking))
    return collapsed


def _signature(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {word for word in words if word not in _STOPWORDS and len(word) > 2}


def _similar(left: set[str], right: set[str], threshold: float = 0.5) -> bool:
    if not left or not right:
        return False
    return len(left & right) / len(left | right) >= threshold