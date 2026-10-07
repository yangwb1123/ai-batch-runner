"""Whole-control width floors and adjacent-column association regressions."""
import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
from ui_fit_widths import control_floor, length_px

spec = importlib.util.spec_from_file_location("ui_fit_gate", SCRIPTS / "check-ui-fit.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def test_number_input_uses_host_floor_not_inner_input():
    markup = '<div data-iris-number-input style="min-width:calc(96px + 2 * var(--iris-padding-sm) + 2px)"><input style="min-width:40px"></div>'
    assert control_floor("IrisNumberInput", markup, {"--iris-padding-sm": "6px"}) == 110
    assert control_floor("IrisNumberInput", '<input style="min-width:40px">', {}) is None


def test_unknown_units_and_expressions_remain_unknown():
    assert length_px("calc(96px + 2 * var(--missing) + 2px)", {}) is None
    assert length_px("min(100%, 180px)", {}) is None
    assert length_px("12rem", {}) is None
    assert length_px("__import__('os').system('id')", {}) is None
    assert length_px("calc(96px + 2 * var(--p, 8px) + 2px)", {}) == 114


def test_explicit_small_size_keeps_whole_floor_and_cell_padding():
    evidence = {"widths_by_size": {"IrisNumberInput": {"sm": 110, "md": 122, "lg": 138}}, "cell_padding": 24}
    assert gate._slot_minimum("IrisNumberInput", '<IrisNumberInput size="sm" />', {}, evidence) == 134
    assert gate._slot_minimum("IrisNumberInput", '<IrisNumberInput size={tier} />', {}, evidence) == 162
    assert gate._slot_minimum("IrisNumberInput", '<IrisNumberInput />', {}, evidence) == 162


def test_sibling_column_module_is_joined_not_silently_skipped(tmp_path):
    sibling = tmp_path / "Phase.columns.ts"
    sibling.write_text("export const columns = [{key:'cost', title:'Cost', width:100}]")
    source = '''import {columns} from './Phase.columns'
const slots = { "cell.cost": () => <IrisNumberInput size="sm" aria-label="Cost" /> }
'''
    path = tmp_path / "Phase.tsx"
    path.write_text(source)
    probe = {"widths_by_size": {"IrisNumberInput": {"sm": 110}}, "cell_padding": 24}
    violations = gate.check_file(path, {"IrisNumberInput": 110}, "Phase.tsx", {"IrisNumberInput": True}, probe=probe)
    assert len(violations) == 1
    assert violations[0]["rule"] == "control-overflow"
    assert "134px" in violations[0]["detail"]
    sibling.write_text("export const columns = [{key:'cost', title:'Cost', width:140}]")
    assert not gate.check_file(path, {"IrisNumberInput": 110}, "Phase.tsx", {"IrisNumberInput": True}, probe=probe)
