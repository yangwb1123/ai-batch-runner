import json
from pathlib import Path

import pytest

from pbatch.migrate import MigrationSpecError, build_report, load_manifest


def _manifest(path: Path, *, cycle: bool = False) -> Path:
    path.write_text(
        """schema_version: migration/v1
name: demo-migration
scope:
  include: ["**/*.py"]
capabilities:
  - id: base
    title: Base capability
    priority: P0
    risk: high
    effort: 10
    source:
      evidence: [src/base.py]
    target:
      evidence: [app/base.py]
    acceptance:
      - id: base-test
        description: base exists
        command: test -f app/base.py
  - id: missing
    title: Missing capability
    depends_on: [base]
    source:
      evidence: [src/missing.py]
    target:
      evidence: [app/missing.py]
    acceptance:
      - id: missing-test
        description: missing exists
        command: test -f app/missing.py
""" if not cycle else """schema_version: migration/v1
name: cycle
capabilities:
  - id: a
    title: A
    depends_on: [b]
    source: {evidence: [a.py]}
  - id: b
    title: B
    depends_on: [a]
    source: {evidence: [b.py]}
""",
        encoding="utf-8",
    )
    return path


def test_compare_reports_evidence_without_claiming_parity(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    (source / "src").mkdir(parents=True)
    (target / "app").mkdir(parents=True)
    (source / "src/base.py").write_text("base\n", encoding="utf-8")
    (source / "src/missing.py").write_text("missing\n", encoding="utf-8")
    (target / "app/base.py").write_text("base\n", encoding="utf-8")
    manifest = _manifest(tmp_path / "migration.yaml")

    report = build_report(str(source), str(target), str(manifest), run_gates=True)

    statuses = {item["id"]: item["status"] for item in report["capabilities"]}
    assert statuses == {"base": "verified", "missing": "missing"}
    assert report["metrics"]["weighted_completion"] == 0.5
    assert report["metrics"]["ready"] is False
    assert report["plan"]["pending"] == 1
    assert report["gates"][0]["passed"] is True


def test_rehearsal_is_bounded_and_budgeted(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    (source / "src").mkdir(parents=True)
    (target / "app").mkdir(parents=True)
    (source / "src/base.py").write_text("base\n", encoding="utf-8")
    (source / "src/missing.py").write_text("missing\n", encoding="utf-8")
    manifest = _manifest(tmp_path / "migration.yaml")

    report = build_report(str(source), str(target), str(manifest), concurrency=2, budget_minutes=5)

    assert report["rehearsal"]["mode"] == "dry_run"
    assert report["rehearsal"]["budget_status"] == "exceeded"
    assert report["plan"]["serial_minutes"] == 40.0
    assert not (target / "app/missing.py").exists()


def test_manifest_rejects_cycles_and_unknown_evidence(tmp_path):
    with pytest.raises(MigrationSpecError, match="dependency cycle"):
        load_manifest(str(_manifest(tmp_path / "cycle.yaml", cycle=True)))

    manifest = tmp_path / "unsafe.yaml"
    manifest.write_text(
        "schema_version: migration/v1\ncapabilities:\n"
        "  - id: unsafe\n    title: Unsafe\n    source: {evidence: [../outside.py]}\n",
        encoding="utf-8",
    )
    with pytest.raises(MigrationSpecError, match="stay relative"):
        load_manifest(str(manifest))


def test_compare_without_manifest_is_inventory_only(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "one.py").write_text("1\n", encoding="utf-8")
    (target / "two.py").write_text("2\n", encoding="utf-8")

    report = build_report(str(source), str(target))

    assert report["metrics"]["capabilities"] == 0
    assert report["metrics"]["ready"] is False
    assert report["inventory"]["source"]["files"] == 1
    assert json.loads(json.dumps(report))["mode"] == "migrate"
