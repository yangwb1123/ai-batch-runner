"""Regression tests for the SVERP no-new-i18n-debt gate."""

from __future__ import annotations

import subprocess
import sys

SCRIPT = "scripts/check-i18n.py"
LOCALES = ("zh-CN", "en-US", "zh-TW", "vi-VN", "id-ID")
MONTH_KEYS = (
    "monthPicker.placeholder",
    "monthPicker.previousYear",
    "monthPicker.nextYear",
    "monthPicker.months",
)


def _run(root, *args):
    return subprocess.run(
        [sys.executable, SCRIPT, "--root", str(root), *args],
        capture_output=True,
        text=True,
        cwd=".",
        timeout=120,
    )


def _catalog(missing: tuple[str, str] | None = None) -> str:
    entries = []
    for locale in LOCALES:
        values = []
        for key in MONTH_KEYS:
            if missing == (locale, key):
                continue
            value = "Select a month in {year}" if key.endswith("months") else key
            values.append(f'    "{key}": "{value}",')
        entries.append(f'  "{locale}": {{\n' + "\n".join(values) + "\n  },")
    iris = "\n".join(entries)
    app = "\n".join(f'  "{locale}": {{\n  }}' for locale in LOCALES)
    return (
        "const irisLocaleMessages = {\n"
        + iris
        + "\n}\n"
        + "const messages = {\n"
        + app
        + "\n}\n"
    )


def _project(tmp_path, catalog: str):
    root = tmp_path
    (root / "apps/web/src").mkdir(parents=True)
    (root / "apps/web/src/i18n.ts").write_text(catalog, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "i18n test"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=root, check=True)
    return root


def test_translated_copy_passes(tmp_path):
    root = _project(tmp_path, _catalog())
    (root / "apps/web/src/Page.tsx").write_text(
        'export const Page = () => <button aria-label={t("page.save")}>{t("page.save")}</button>\n',
        encoding="utf-8",
    )
    result = _run(root)
    assert result.returncode == 0, result.stderr


def test_new_literal_copy_fails(tmp_path):
    root = _project(tmp_path, _catalog())
    (root / "apps/web/src/Page.tsx").write_text(
        'export const Page = () => <button aria-label="关闭">保存</button>\n',
        encoding="utf-8",
    )
    result = _run(root)
    assert result.returncode == 1
    assert "hard-coded" in result.stderr


def test_fixed_locale_fails(tmp_path):
    root = _project(tmp_path, _catalog())
    (root / "apps/web/src/Page.tsx").write_text(
        '<IrisMonthPicker locale="zh-CN" />\n',
        encoding="utf-8",
    )
    result = _run(root)
    assert result.returncode == 1
    assert "fixed locale" in result.stderr


def test_missing_iris_translation_fails(tmp_path):
    root = _project(tmp_path, _catalog(("vi-VN", "monthPicker.nextYear")))
    result = _run(root)
    assert result.returncode == 1
    assert "vi-VN" in result.stderr
    assert "monthPicker.nextYear" in result.stderr


def test_test_sources_are_not_copy_targets(tmp_path):
    root = _project(tmp_path, _catalog())
    (root / "apps/web/src/Page.test.tsx").write_text(
        'test("保存", () => expect("关闭").toBe("关闭"))\n',
        encoding="utf-8",
    )
    result = _run(root)
    assert result.returncode == 0, result.stderr
