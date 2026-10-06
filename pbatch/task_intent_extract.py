"""Deterministic lexical hints, not semantic drift verdicts or executable plans."""
from __future__ import annotations

import re
import time
from collections import Counter

from .task_center_io import display
from .task_intent_schema import fingerprint

ALGORITHM = "lexical-hints-v1"
_RULES = {
    "goal": re.compile(r"目标|实现|修复|开发|构建|\b(?:goal|build|implement|fix|want)\b", re.I),
    "constraints": re.compile(r"不要|禁止|不得|必须|保留|不允许|\b(?:must|never|without|do not)\b", re.I),
    "focus": re.compile(r"当前|现在|本轮|优先|重点|\b(?:currently|focus|priority)\b", re.I),
    "next": re.compile(r"下一步|接下来|之后|随后|\b(?:next|then|afterwards)\b", re.I),
}
_CHANGE = re.compile(r"改为|转向|不再|换成|\b(?:instead|change direction|no longer)\b", re.I)
_SPLIT = re.compile(r"[^\n。！？.!?]+[。！？.!?]?")
MAX_HINTS = 8


def project(value: dict) -> dict:
    began = time.perf_counter()
    hints = {key: [] for key in _RULES}
    revisions, previous = [], None
    counts = Counter(message["role"] for message in value["messages"])
    for message in value["messages"]:
        if message["role"] != "user":
            continue  # assistant/planner/tool prose cannot become the user's goal
        citation = {"history_id": value["intent_id"], "message_id": display(message["id"], 128),
                    "message_sha256": fingerprint(message), "ts": message["ts"], "role": "user"}
        for match in _SPLIT.finditer(message["text"]):
            fragment = match.group().strip()
            if not fragment:
                continue
            for category, rule in _RULES.items():
                if rule.search(fragment):
                    hints[category].append({"excerpt": display(fragment, 280),
                        "citation": {**citation, "offset": match.start(), "end_offset": match.end(),
                                     "fragment_sha256": fingerprint(match.group())},
                        "excerpt_is_redacted_and_bounded": True,
                        "confidence": "low_lexical_only", "status": "candidate_not_authority"})
        if _RULES["goal"].search(message["text"]):
            current = {"citation": citation, "text_digest": fingerprint(message["text"])}
            if previous and previous["text_digest"] != current["text_digest"]:
                revisions.append({"kind": "scope_change_cue" if _CHANGE.search(message["text"])
                    else "goal_restatement", "previous": previous["citation"], "current": citation,
                    "status": "unconfirmed", "semantic_drift": "not_evaluated"})
            previous = current
    selected = {category: entries[-MAX_HINTS:] for category, entries in hints.items()}
    return {"method": ALGORITHM, "status": "candidate_inferences", "hints": selected,
            "revisions": revisions[-MAX_HINTS:], "role_counts": dict(counts),
            "omitted_hints": sum(max(0, len(entries) - MAX_HINTS) for entries in hints.values()),
            "semantic_drift": {"status": "not_evaluated", "note": "lexical changes are not proof of drift"},
            "usage": {"llm_requests": 0, "total_tokens": 0, "local_compute_seconds": time.perf_counter() - began,
                "cost_total": None, "scope": "this_deterministic_projection_only",
                "note": "no model invoked; local compute is measured but not priced"},
            "grants_execution": False}
