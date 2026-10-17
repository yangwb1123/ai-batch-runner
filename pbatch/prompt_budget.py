"""Bound aggregated evidence inside a rendered prompt without hiding the cut.

The default policy stays fail-closed (``prompt.overflow = fail``): an oversized
prompt is rejected by the runner exactly as before.  ``truncate`` is an
explicit opt-in that keeps the instruction template intact and clamps only the
injected evidence, leaving a visible marker plus a head/tail window so the
agent still sees the start and end of the evidence.
"""

from __future__ import annotations

from typing import Optional

from . import config

EVIDENCE_TRUNCATION_MARKER = (
    "\n[evidence truncated: prompt byte budget reached]\n")
_HEAD_RATIO = 3


def _truncate_utf8(text: str, budget: int) -> str:
    raw = text.encode("utf-8")
    if budget <= 0:
        return ""
    if len(raw) <= budget:
        return text
    head_budget = max(0, budget * _HEAD_RATIO // 4)
    tail_budget = max(0, budget - head_budget)
    head = raw[:head_budget].decode("utf-8", "ignore") if head_budget else ""
    tail = (raw[len(raw) - tail_budget:].decode("utf-8", "ignore")
            if tail_budget else "")
    return head + EVIDENCE_TRUNCATION_MARKER + tail


def fit_prompt_evidence(
        evidence: str, rendered: str, *,
        limit: Optional[int] = None, policy: Optional[str] = None,
        reserve_bytes: int = 0) -> str:
    """Return ``rendered`` bounded to the configured prompt budget.

    ``reserve_bytes`` accounts for text the caller prepends after this helper
    (for example the re-verified upstream-artifact block), so the final argv
    value still fits the runner's budget.
    """
    limit = config.PROMPT_MAX_BYTES if limit is None else limit
    policy = config.PROMPT_OVERFLOW if policy is None else policy
    if limit <= 0 or policy != "truncate" or not evidence:
        return rendered
    allowed = limit - max(0, int(reserve_bytes or 0))
    if allowed <= 0:
        return rendered
    if len(rendered.encode("utf-8")) <= allowed:
        return rendered
    without = rendered.replace(evidence, "", 1)
    marker_bytes = len(EVIDENCE_TRUNCATION_MARKER.encode("utf-8"))
    available = allowed - len(without.encode("utf-8")) - marker_bytes
    if available <= 0:
        return rendered
    replacement = _truncate_utf8(evidence, available)
    if replacement == evidence:
        return rendered
    bounded = rendered.replace(evidence, replacement, 1)
    if len(bounded.encode("utf-8")) > allowed:
        return rendered
    return bounded
