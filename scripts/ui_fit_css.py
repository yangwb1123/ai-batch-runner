"""CSS half of the UI fit gate: stylesheets that cancel a control's own minimum.

Kept separate from ``check-ui_fit`` so each file stays inside the repository's
500-line budget, and because the two halves read different languages: this one
parses CSS, its sibling parses TSX/TSX expressions.

Why this rule exists (measured, not guessed): the APS pages shipped rules like

    .aps-settings-company-select[data-iris-select-trigger] {
      width: 8.5rem !important;
      min-width: 0 !important;
    }

``min-width: 0 !important`` also cancels the minimum the component ships
(IrisSelect declares ``min-width:140px``). On a tight container the trigger is
then squeezed narrower than its own text, and the label/option text spills out
of the control -- exactly the "label overflows the control" report.

Iris writes its ``data-iris-*`` attributes on the element that actually carries
the interaction, so a selector matching one of those attributes *is* a rule about
that control. No name-guessing table is needed.
"""

from __future__ import annotations

import re
from typing import Optional

# data-iris-* attribute -> the component that renders it.
TRIGGER_ATTRS = {
    "data-iris-select-trigger": "IrisSelect",
    "data-iris-month-picker-trigger": "IrisMonthPicker",
    "data-iris-number-input": "IrisNumberInput",
    "data-iris-input": "IrisInput",
    "data-iris-switch": "IrisSwitch",
    "data-iris-checkbox": "IrisCheckbox",
    "data-iris-date-picker-trigger": "IrisDatePicker",
}

_ZERO_MIN_WIDTH = re.compile(r"min-width:\s*0\s*(?:!important)?\s*;")
_COMMENTS = re.compile(r"/\*.*?\*/", re.S)
_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")


def rules(source: str) -> list[tuple[str, str]]:
    """(selector, declaration block) for every flat CSS rule."""
    return [
        (match.group(1).strip(), match.group(2))
        for match in _RULE.finditer(_COMMENTS.sub("", source))
    ]


def control_of(selector: str) -> Optional[str]:
    for attribute, control in TRIGGER_ATTRS.items():
        if attribute in selector:
            return control
    return None


def min_width_overrides(
    source: str, widths: dict[str, int], relative: str
) -> list[dict]:
    """Rules that drive a control's minimum width to zero."""
    violations: list[dict] = []
    for selector, block in rules(source):
        control = control_of(selector)
        if control is None:
            continue
        minimum = widths.get(control)
        if minimum is None or not _ZERO_MIN_WIDTH.search(block):
            continue
        violations.append({
            "file": relative,
            "line": source.count("\n", 0, source.find(selector)) + 1,
            "rule": "control-min-width-override",
            "detail": (
                f"{selector} 把 {control} 的 min-width 压到 0，"
                f"但该控件声明的下限是 {minimum}px"
            ),
        })
    return violations
