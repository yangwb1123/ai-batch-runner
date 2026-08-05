from pathlib import Path

from pbatch.models import Stage
from pbatch.relevance import (RoleScore, constrain_role_plan,
                              format_suggestions, load_role_keywords,
                              score_roles)
from pbatch import meta


def test_score_roles_uses_latin_boundaries_and_cjk_substrings():
    keywords = {
        "security": ["auth", "安全", "token"],
        "qa": ["test"],
    }
    scores = score_roles("authentication 安全 tokenization; auth token", ["qa", "security"], keywords)
    assert scores[0] == RoleScore("security", 6, ("auth", "安全", "token"))
    assert scores[1].score == 0


def test_score_roles_is_stable_and_capped():
    keywords = {"a": list("abcdef"), "b": ["x"]}
    scores = score_roles("a b c d e f x", ["b", "a"], keywords)
    assert [(item.role, item.score) for item in scores] == [("a", 10), ("b", 2)]


def test_format_suggestions_includes_evidence():
    text = format_suggestions([RoleScore("qa", 4, ("test", "验收"))])
    assert "qa: 4" in text
    assert "test, 验收" in text


def test_constrain_plan_dedupes_filters_and_hard_caps():
    plan = [
        {"role": "architect", "task": ""},
        {"role": "ARCHITECT", "task": ""},
        {"role": "qa", "task": ""},
        {"role": "custom", "task": "Do a focused review"},
        {"role": "security", "task": ""},
    ]
    scores = [RoleScore("architect", 4, ("design",)), RoleScore("qa", 0, ())]
    kept, dropped = constrain_role_plan(plan, 2, scores, minimum=2)
    assert [item["role"] for item in kept] == ["architect", "custom"]
    assert set(dropped) == {"ARCHITECT", "qa", "security"}


def test_default_keywords_load_from_package():
    keywords = load_role_keywords()
    assert "security_engineer" in keywords
    assert "安全" in keywords["security_engineer"]


def test_malformed_keyword_yaml_disables_scoring(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("roles: [unterminated\n", encoding="utf-8")
    assert load_role_keywords(str(path)) == {}


def test_custom_meta_prompt_gets_suggestions_appended():
    stage = Stage(name="review", meta_prompt="Roles: {roles}\n{input_content}")
    task = meta._build_meta_prompt_task(
        stage, ["security_engineer"], "security token authentication", "", 0)
    assert "Relevance suggestions" in task.prompt
    assert "security_engineer" in task.prompt


def test_role_output_sanitize_collision_is_disambiguated(tmp_path: Path):
    stage = Stage(name="review", output_dir=str(tmp_path))
    roles = [
        {"role": "security/engineer", "task": "one"},
        {"role": "security.engineer", "task": "two"},
    ]
    tasks, ok = meta._build_role_tasks(stage, roles, "context", "", 0)
    names = [Path(task.output).name for _, task in tasks]
    assert ok is True
    assert names[0] == "security_engineer.md"
    assert names[1].startswith("security_engineer-")
    assert len(set(names)) == 2
