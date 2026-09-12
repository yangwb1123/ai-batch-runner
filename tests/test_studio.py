import json

import pytest

from pbatch.studio import StudioSpecError, build_report, load_spec


def _spec(path, cycle=False):
    path.write_text(
        """schema_version: studio/v1
name: aps-studio
brief: APS design ledger
constraints: [source-first, unknown-is-not-zero]
artifacts:
  - id: requirements
    type: requirements
    path: docs/requirements.json
    required: [goal, personas, flows, acceptance, non_goals]
  - id: domain
    type: domain_model
    path: docs/domain.json
    required: [entities, invariants]
    evidence_required: true
stages:
  - id: requirements
    role: requirement
    artifacts: [requirements]
    depends_on: []
    effort: 20
  - id: domain
    role: domain
    artifacts: [domain]
    depends_on: [requirements]
    effort: 30
""" if not cycle else """schema_version: studio/v1
name: cycle
artifacts:
  - id: a
    type: a
    path: a.json
  - id: b
    type: b
    path: b.json
stages:
  - id: a
    role: a
    artifacts: [a]
    depends_on: [b]
  - id: b
    role: b
    artifacts: [b]
    depends_on: [a]
""",
        encoding="utf-8",
    )
    return path


def _requirements():
    return {
        "schema_version": "studio/v1",
        "artifact_type": "requirements",
        "status": "verified",
        "goal": "plan production",
        "personas": ["planner"],
        "flows": ["allocate"],
        "acceptance": ["quantity invariant"],
        "non_goals": ["replace ERP"],
        "evidence": [{"path": "docs/source.md"}],
        "verification": {"passed": True},
    }


def test_studio_reports_artifact_readiness_and_waves(tmp_path):
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / "docs/source.md").write_text("source evidence", encoding="utf-8")
    (project / "docs/requirements.json").write_text(
        json.dumps(_requirements()), encoding="utf-8")
    spec = _spec(tmp_path / "studio.yaml")

    report = build_report(str(project), str(spec), concurrency=2, budget_minutes=10)

    assert report["metrics"]["counts"]["verified"] == 1
    assert report["metrics"]["counts"]["missing"] == 1
    assert report["metrics"]["implementation_ready"] is False
    assert report["plan"]["items"][0]["id"] == "domain"
    assert report["rehearsal"]["budget_status"] == "exceeded"


def test_studio_rejects_cycle_and_fabricated_verified(tmp_path):
    with pytest.raises(StudioSpecError, match="dependency cycle"):
        load_spec(str(_spec(tmp_path / "cycle.yaml", cycle=True)))

    project = tmp_path / "project"
    project.mkdir()
    artifact = project / "a.json"
    artifact.write_text(json.dumps({"status": "verified", "evidence": []}), encoding="utf-8")
    spec = tmp_path / "fake.yaml"
    spec.write_text(
        "schema_version: studio/v1\nname: fake\nartifacts:\n"
        "  - id: a\n    type: a\n    path: a.json\n    required: []\nstages:\n"
        "  - id: a\n    role: a\n    artifacts: [a]\n",
        encoding="utf-8",
    )

    report = build_report(str(project), str(spec))

    assert report["artifacts"][0]["state"] == "invalid"
    assert "verification.passed" in report["artifacts"][0]["errors"][0]


def test_studio_is_read_only_without_out(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    spec = _spec(tmp_path / "studio.yaml")

    before = sorted(path.relative_to(project).as_posix() for path in project.rglob("*"))
    build_report(str(project), str(spec))
    after = sorted(path.relative_to(project).as_posix() for path in project.rglob("*"))

    assert before == after
