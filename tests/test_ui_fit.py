"""Tests for scripts/check-ui-fit.py — the UI fit & accessible-name gate.

The gate only earns trust if it is provably quiet on good code and loud on
the two defect classes it claims to catch, so each rule gets a positive and a
negative case, plus a case for the property that matters most: when the
installed component package no longer matches the recorded evidence, the
width rule must go silent rather than report an undefendable number.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = str(Path(__file__).resolve().parent.parent / "scripts" / "check-ui-fit.py")

# A stand-in for @iris-ui-kit/vue's dist: the same top-level definition shape,
# carrying the trigger styles the registry cites as evidence.
FAKE_DIST = """
var IrisSelect = defineComponent({
  name: "IrisSelect",
  setup() {
    return () => h("button", { style: { position: "relative", width: "auto", minWidth: "140px" } })
  },
})
var IrisDatePicker = defineComponent({
  name: "IrisDatePicker",
  setup() {
    return () => h("button", { style: { minHeight: "34px", minWidth: "180px" } })
  },
})
var IrisSwitch = defineComponent({
  name: "IrisSwitch",
  setup() {
    const sizes = { sm: { width: "28px", height: "16px", thumb: "12px" },
                    md: { width: "36px", height: "20px", thumb: "16px" },
                    lg: { width: "44px", height: "24px", thumb: "20px" } }
    return () => h("span", sizes.lg)
  },
})
"""


def _run(*args, cwd=None):
    return subprocess.run(
        [sys.executable, SCRIPT, *args],
        capture_output=True, text=True, timeout=120, cwd=cwd,
    )


def _project(root, files: dict, dist: str = FAKE_DIST) -> None:
    pkg = root / "node_modules/@iris-ui-kit/vue/dist"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "index.js").write_text(dist, encoding="utf-8")
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _json(root, *extra):
    # 真实用法是"在仓库根跑、对相对路径 --dir"，所以 cwd 必须是被测项目根。
    result = _run("--dir", "src", "--json", *extra, cwd=str(root))
    return result, json.loads(result.stdout)


def test_narrow_column_for_date_picker_is_reported(tmp_path):
    _project(tmp_path, {
        "src/Due.tsx": """
export const due = {
  columns: [{ key: "due", title: "Due", width: 100 }],
  slots: {
    "cell.due": ({ row }) => (
      <IrisDatePicker modelValue={null} onUpdate:modelValue={() => undefined} />
    ),
  },
}
""",
    })
    result, payload = _json(tmp_path)
    assert result.returncode == 1, result.stdout
    overflow = [v for v in payload["violations"] if v["rule"] == "control-overflow"]
    assert len(overflow) == 1
    assert "180px" in overflow[0]["detail"]
    assert payload["control_min_widths"]["IrisDatePicker"] == 180


def test_wide_column_for_the_same_control_is_clean(tmp_path):
    _project(tmp_path, {
        "src/Due.tsx": """
export const due = {
  columns: [{ key: "due", title: "Due", width: 220 }],
  slots: {
    "cell.due": ({ row }) => (
      <IrisDatePicker
        modelValue={null}
        aria-label="Due date"
        onUpdate:modelValue={() => undefined}
      />
    ),
  },
}
""",
    })
    result, payload = _json(tmp_path)
    assert result.returncode == 0, payload["violations"]
    assert payload["violations"] == []


def test_switch_uses_its_own_track_width_not_a_neighbouring_component(tmp_path):
    """28/36/44 is the switch; 140 belongs to the select trigger.

    A fixed-window scrape reads the switch back as 140px and then demands a
    140px column for every toggle cell. The registry is scoped to the
    component's own definition so this cannot happen.
    """
    # 同时给出 id + <label for>，让这个用例只考察宽度（a11y 由其它用例覆盖）。
    _project(tmp_path, {
        "src/Flag.tsx": """
const FLAG_ID = "flag-1"
export const flag = {
  columns: [{ key: "flag", title: "Flag", width: 60 }],
  slots: {
    "cell.flag": ({ row }) => (
      <div>
        <label for={FLAG_ID}>Flag</label>
        <IrisSwitch id={FLAG_ID} modelValue={false} aria-label="Flag" />
      </div>
    ),
  },
}
""",
    })
    result, payload = _json(tmp_path)
    assert result.returncode == 0, payload["violations"]
    assert payload["control_min_widths"]["IrisSwitch"] == 44


def test_unverifiable_registry_entry_makes_the_width_rule_go_silent(tmp_path):
    """Wrong evidence -> the entry is dropped, not believed."""
    _project(tmp_path, {
        "src/Due.tsx": """
export const due = {
  columns: [{ key: "due", title: "Due", width: 100 }],
  slots: {
    "cell.due": ({ row }) => (
      <IrisDatePicker modelValue={null} aria-label="Due" />
    ),
  },
},
""",
    }, dist='var IrisDatePicker = defineComponent({\n  name: "IrisDatePicker",\n})\n')
    result, payload = _json(tmp_path)
    assert "IrisDatePicker" not in payload["control_min_widths"]
    assert payload["width_source"] == "unverified"
    # No width violation is asserted on an unverified number ...
    assert not [v for v in payload["violations"] if v["rule"] == "control-overflow"]
    # ... and the accessible-name rule still applies.
    assert result.returncode == 0, payload["violations"]


def test_accessible_name_accepts_a_label_ancestor(tmp_path):
    _project(tmp_path, {
        "src/Form.tsx": """
export const form = (
  <label class="field">
    <span>Name</span>
    <IrisInput modelValue="" onUpdate:modelValue={() => undefined} />
  </label>
)
""",
    })
    result, payload = _json(tmp_path)
    assert result.returncode == 0, payload["violations"]


def test_accessible_name_accepts_aria_label(tmp_path):
    _project(tmp_path, {
        "src/Form.tsx": """
export const form = () => (
  <IrisInput modelValue="" aria-label="Name" onUpdate:modelValue={() => undefined} />
)
""",
    })
    result, payload = _json(tmp_path)
    assert result.returncode == 0, payload["violations"]


def test_accessible_name_reports_an_unlabelled_control(tmp_path):
    _project(tmp_path, {
        "src/Form.tsx": """
export const form = () => (
  <IrisInput modelValue="" onUpdate:modelValue={() => undefined} />
)
""",
    })
    result, payload = _json(tmp_path)
    assert result.returncode == 1
    names = [v for v in payload["violations"] if v["rule"] == "control-accessible-name"]
    assert len(names) == 1
    assert "IrisInput" in names[0]["detail"]


def test_test_files_and_testkits_are_not_scanned(tmp_path):
    _project(tmp_path, {
        "src/Widget.test.tsx": "export const w = () => <IrisInput modelValue=\"\" />\n",
        "src/__testkit__/kit.tsx": "export const k = () => <IrisInput modelValue=\"\" />\n",
        "src/Widget.tsx": "export const w = () => <div />\n",
    })
    result, payload = _json(tmp_path)
    assert result.returncode == 0, payload["violations"]
    assert payload["files"] == 1


def test_label_mentioned_only_in_a_comment_does_not_open_a_phantom_scope(tmp_path):
    """回归：注释里写 `<label for>` 曾让整文件的 label 计数失衡。"""
    _project(tmp_path, {
        "src/Form.tsx": """/**
 * 与 <label for> 配对的稳定 id。
 */
export const form = () => (
  <div>
    <label class="field">
      <span>Name</span>
      <IrisInput modelValue="" onUpdate:modelValue={() => undefined} />
    </label>
  </div>
)
""",
    })
    result, payload = _json(tmp_path)
    assert result.returncode == 0, payload["violations"]


def test_wrapping_label_with_text_names_the_switch_even_with_an_inert_aria_label(tmp_path):
    """IrisSwitch 的 aria-label 落在包裹 <label> 上，但该 label 的文本已经命名了它。"""
    _project(tmp_path, {
        "src/Toggle.tsx": """
export const toggle = () => (
  <label class="field">
    <span>Append mode</span>
    <IrisSwitch modelValue={false} aria-label="Append mode" />
  </label>
)
""",
    })
    result, payload = _json(tmp_path)
    assert result.returncode == 0, payload["violations"]


def test_aria_label_on_a_wrapper_only_control_is_still_reported(tmp_path):
    """没有任何命名通路时必须报出来。"""
    _project(tmp_path, {
        "src/Toggle.tsx": """
export const toggle = () => (
  <div>
    <IrisSwitch modelValue={false} aria-label="Append mode" />
  </div>
)
""",
    })
    result, payload = _json(tmp_path)
    assert result.returncode == 1
    assert any("落在包裹元素上" in v["detail"] for v in payload["violations"])


def test_css_driving_a_control_min_width_to_zero_is_reported(tmp_path):
    """IrisSelect 声明 min-width:140px；页面再写 min-width:0 就等于取消它。"""
    _project(tmp_path, {
        "src/app.css": """
.company-select[data-iris-select-trigger] {
  width: 8.5rem !important;
  min-width: 0 !important;
}
""",
    })
    result, payload = _json(tmp_path)
    assert result.returncode == 1, payload
    rules = [v for v in payload["violations"] if v["rule"] == "control-min-width-override"]
    assert len(rules) == 1
    assert "140px" in rules[0]["detail"]


def test_css_keeping_a_real_min_width_is_clean(tmp_path):
    _project(tmp_path, {
        "src/app.css": """
.company-select[data-iris-select-trigger] {
  width: 100% !important;
  min-width: 9rem !important;
}
""",
    })
    result, payload = _json(tmp_path)
    assert result.returncode == 0, payload["violations"]


def test_css_rule_for_an_unrelated_class_is_ignored(tmp_path):
    _project(tmp_path, {
        "src/app.css": """
.some-grid > div {
  min-width: 0 !important;
}
""",
    })
    result, payload = _json(tmp_path)
    assert result.returncode == 0, payload["violations"]
