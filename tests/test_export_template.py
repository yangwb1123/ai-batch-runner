"""Contract tests for the portable specification pack exporter."""

from __future__ import annotations

import json

from pbatch.export_template import export_main


def test_default_export_is_self_validating(tmp_path):
    out = tmp_path / "pack"
    assert export_main(["-o", str(out)]) == 0
    manifest = json.loads((out / "VERSION.json").read_text(encoding="utf-8"))
    readme = (out / "README.md").read_text(encoding="utf-8")
    assert len(manifest["checkers"]) == 3
    assert "scripts/check-design-intelligence.py" in readme
    assert (out / "scripts" / "check-design-intelligence.py").is_file()
    assert manifest["sha256"]["scripts/check-design-intelligence.py"]
    assert all(not item.startswith("/") for item in manifest["specs"])


def test_no_checkers_readme_does_not_claim_checker_commands(tmp_path):
    out = tmp_path / "pack"
    assert export_main(["--no-checkers", "-o", str(out)]) == 0
    manifest = json.loads((out / "VERSION.json").read_text(encoding="utf-8"))
    readme = (out / "README.md").read_text(encoding="utf-8")
    assert manifest["checkers"] == []
    assert "未包含检查器" in readme
    assert "--selfcheck" not in readme


def test_specs_override_defaults_and_accept_comma_separated_paths(tmp_path):
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("one", encoding="utf-8")
    second.write_text("two", encoding="utf-8")
    out = tmp_path / "pack"
    specs = f"{first},{second}"
    assert export_main(["--specs", specs, "--no-checkers",
                        "-o", str(out)]) == 0
    manifest = json.loads((out / "VERSION.json").read_text(encoding="utf-8"))
    assert manifest["specs"] == ["first.md", "second.md"]
    assert (out / "first.md").read_text(encoding="utf-8") == "one"
    assert (out / "second.md").read_text(encoding="utf-8") == "two"


def test_missing_asset_and_nonempty_output_fail_closed(tmp_path, capsys):
    missing_out = tmp_path / "missing-pack"
    assert export_main(["--specs", str(tmp_path / "missing.md"),
                        "--no-checkers", "-o", str(missing_out)]) == 2
    assert not missing_out.exists()
    assert "missing export assets" in capsys.readouterr().err

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "old.txt").write_text("stale", encoding="utf-8")
    spec = tmp_path / "spec.md"
    spec.write_text("spec", encoding="utf-8")
    assert export_main(["--specs", str(spec), "--no-checkers",
                        "-o", str(occupied)]) == 2
    assert (occupied / "old.txt").read_text(encoding="utf-8") == "stale"


def test_output_cannot_be_nested_in_exported_directory(tmp_path):
    source = tmp_path / "specs"
    source.mkdir()
    (source / "rule.md").write_text("rule", encoding="utf-8")
    out = source / "pack"
    assert export_main(["--specs", str(source), "--no-checkers",
                        "-o", str(out)]) == 2
    assert not out.exists()
