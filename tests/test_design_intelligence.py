"""Design Intelligence gate tests: checker behavior + rule registry."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "check-design-intelligence.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("cdi", CHECKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cdi():
    return _load_checker()


def _check(cdi, name: str, source: str):
    return cdi._check_file(Path(name), source)


class TestSemanticColors:
    def test_raw_semantic_hex_is_violation(self, cdi):
        src = "Container(color: Color(0x10B981))"
        kinds = [v["check"] for v in _check(cdi, "s.dart", src)]
        assert "semantic_color_literal" in kinds

    def test_colors_green_is_violation(self, cdi):
        src = "Icon(color: Colors.green)"
        kinds = [v["check"] for v in _check(cdi, "s.dart", src)]
        assert "semantic_color_literal" in kinds

    def test_token_file_is_exempt(self, cdi):
        src = "static const success = Color(0x10B981);"
        assert _check(cdi, "app_colors.dart", src) == []


class TestKpiEmphasis:
    def test_kpi_page_without_emphasis(self, cdi):
        src = (
            "class X extends StatelessWidget { Widget build(BuildContext context) {"
            " return Text('98%'); } }"
        )
        kinds = [v["check"] for v in _check(cdi, "overview_tab.dart", src)]
        assert "kpi_emphasis_missing" in kinds

    def test_kpi_page_with_emphasis_ok(self, cdi):
        src = (
            "class X extends StatelessWidget { Widget build(BuildContext context) {"
            " return Text('98%', style: TextStyle(fontSize: 32)); } }"
        )
        assert _check(cdi, "overview_tab.dart", src) == []

    def test_detail_page_not_checked(self, cdi):
        src = "Text('98%') Widget build"
        assert _check(cdi, "client_detail_screen.dart", src) == []


class TestEmptyState:
    def test_bare_empty_violation(self, cdi):
        src = "Widget build { return EmptyState(title: 'No data'); }"
        kinds = [v["check"] for v in _check(cdi, "s.dart", src)]
        assert "empty_state_no_action" in kinds

    def test_empty_with_action_ok(self, cdi):
        src = (
            "Widget build { return EmptyState(title: 'No data',"
            " actionLabel: 'Create', onAction: _create); }"
        )
        assert _check(cdi, "s.dart", src) == []


class TestDualEncoding:
    def test_bare_status_violation(self, cdi):
        src = "Widget build { Text('Active'); }"
        kinds = [v["check"] for v in _check(cdi, "s.dart", src)]
        assert "status_not_dual_encoded" in kinds

    def test_chip_dual_encoded_ok(self, cdi):
        src = "Widget build { StatusChip.active(); Text('Active'); }"
        assert _check(cdi, "s.dart", src) == []

    def test_form_option_exempt(self, cdi):
        src = "Widget build { SwitchListTile(title: Text('Active')); }"
        assert _check(cdi, "s.dart", src) == []


class TestSelfcheck:
    def test_selfcheck_passes(self):
        result = subprocess.run(
            [sys.executable, str(CHECKER), "--selfcheck"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout
        assert "OK" in result.stdout


class TestRegistrySelection:
    def test_production_workbench_gets_full_di_set(self):
        sys.path.insert(0, str(ROOT))
        from pbatch.rule_matcher import match_rules

        r = match_rules("企业订单管理工作台，含 KPI 与趋势图表，生产环境")
        ids = [x["id"] for x in r["rules"]]
        di = [i for i in ids if i.startswith("design-intelligence")]
        assert {
            "design-intelligence-product",
            "design-intelligence-color",
            "design-intelligence-priority",
            "design-intelligence-cognitive",
            "design-intelligence-emotional",
            "design-intelligence-data",
        } <= set(di)

    def test_demo_gets_core_only(self):
        sys.path.insert(0, str(ROOT))
        from pbatch.rule_matcher import match_rules

        r = match_rules("做一个登录页 demo")
        ids = [x["id"] for x in r["rules"]]
        di = [i for i in ids if i.startswith("design-intelligence")]
        assert set(di) <= {
            "design-intelligence-product",
            "design-intelligence-priority",
        }
