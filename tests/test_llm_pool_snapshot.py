"""Loaded-state comparison is pure, canonical, bounded by validated pool inputs."""

from dataclasses import fields, replace
import hashlib
import json
from types import MappingProxyType

import pytest

from pbatch.llm_pool_config import PoolConfig, load_config
from pbatch.llm_pool_snapshot import snapshot_tag, valid_tag
from test_llm_pool_config import pool_file


TOKEN = "test-only-snapshot-token-0123456789"
KEYS = {"go-a": "test-only-snapshot-key-a-0123456789", "go-b": "test-only-snapshot-key-b-0123456789"}


def test_snapshot_never_reads_files_or_creates_runtime(pool_file, monkeypatch):
    path, _ = pool_file
    settings = load_config(str(path))
    def forbidden(*args, **kwargs):
        pytest.fail("snapshot comparison must not perform I/O")
    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr("pathlib.Path.open", forbidden)
    monkeypatch.setattr("socket.socket", forbidden)
    tag = snapshot_tag(settings, TOKEN, KEYS)
    assert valid_tag(tag) and len(tag) == 67
    assert all(secret not in tag for secret in (TOKEN, *KEYS.values(), str(path.parent)))
    assert all(hashlib.sha256(key.encode()).hexdigest() not in tag for key in KEYS.values())
    assert not settings.state_file.exists()


@pytest.mark.parametrize("field,value", [
    ("pool_id", "another-pool"), ("port", 9000), ("request_timeout", 10), ("probe_after", 10),
    ("max_connections", 2), ("usage_enabled", True), ("usage_max_age", 10), ("usage_timeout", 2),
    ("token_file", "another-token"), ("state_file", "another-state"),
    ("models", {"glm-5.1": "messages", "minimax-m2.7": "messages"}),
])
def test_every_configuration_section_affects_snapshot(pool_file, field, value):
    path, _ = pool_file
    settings = load_config(str(path))
    if field.endswith("_file"):
        value = path.parent / value
    assert snapshot_tag(settings, TOKEN, KEYS) != snapshot_tag(replace(settings, **{field: value}), TOKEN, KEYS)


@pytest.mark.parametrize("field,value", [("id", "renamed"), ("quota_group", "another-group"),
                                       ("credential_file", "/new/key"), ("priority", 1),
                                       ("max_concurrent", 2), ("enabled", False)])
def test_all_account_fields_and_loaded_key_binding_affect_snapshot(pool_file, field, value):
    path, _ = pool_file
    settings = load_config(str(path))
    first = replace(settings.accounts[0], **{field: value})
    changed = replace(settings, accounts=(first, settings.accounts[1]))
    keys = dict(KEYS)
    if field == "id":
        keys[value] = keys.pop("go-a")
    elif field == "enabled":
        keys.pop("go-a")
    assert snapshot_tag(settings, TOKEN, KEYS) != snapshot_tag(changed, TOKEN, keys)


def test_account_order_and_membership_are_significant(pool_file):
    path, _ = pool_file
    settings = load_config(str(path))
    tag = snapshot_tag(settings, TOKEN, KEYS)
    assert tag != snapshot_tag(replace(settings, accounts=settings.accounts[::-1]), TOKEN, KEYS)
    assert tag != snapshot_tag(replace(settings, accounts=settings.accounts[:1]), TOKEN, {"go-a": KEYS["go-a"]})


def test_same_path_key_rotation_key_swap_and_local_token_rotation_are_detectable(pool_file):
    path, _ = pool_file
    settings = load_config(str(path))
    tag = snapshot_tag(settings, TOKEN, KEYS)
    assert tag != snapshot_tag(settings, TOKEN, {**KEYS, "go-a": "test-rotated-0123456789"})
    assert tag != snapshot_tag(settings, TOKEN, {"go-a": KEYS["go-b"], "go-b": KEYS["go-a"]})
    assert tag != snapshot_tag(settings, "different-local-token-0123456789", KEYS)


def test_equivalent_model_mapping_order_numeric_representation_and_readonly_maps_match(pool_file):
    path, _ = pool_file
    settings = load_config(str(path))
    equivalent = replace(settings, models=MappingProxyType(dict(reversed(list(settings.models.items())))),
                         request_timeout=300.0, probe_after=60.0, usage_max_age=60.0, usage_timeout=5.0)
    assert snapshot_tag(settings, TOKEN, KEYS) == snapshot_tag(equivalent, TOKEN, MappingProxyType(KEYS))
    assert snapshot_tag(settings, TOKEN, KEYS, port=9001) == snapshot_tag(replace(settings, port=9001), TOKEN, KEYS)


def test_yaml_comments_defaults_and_field_order_do_not_create_false_drift(pool_file):
    path, data = pool_file
    settings = load_config(str(path))
    data["gateway"].update(port=8741, request_timeout=300.0, probe_after=60, max_connections=16)
    data["usage"] = {"enabled": False, "max_age": 60.0, "timeout": 5.0}
    for account in data["accounts"]:
        account.update(enabled=True, priority=0, max_concurrent=1)
    path.write_text("# changed formatting only\n" + json.dumps(data), encoding="utf-8")
    assert snapshot_tag(settings, TOKEN, KEYS) == snapshot_tag(load_config(str(path)), TOKEN, KEYS)


@pytest.mark.parametrize("keys", [None, {}, {"go-a": KEYS["go-a"]}, {**KEYS, "extra": "unused"}])
def test_incomplete_or_extra_credentials_cannot_be_attested(pool_file, keys):
    path, _ = pool_file
    with pytest.raises(ValueError, match="complete enabled credentials"):
        snapshot_tag(load_config(str(path)), TOKEN, keys)


def test_disabled_credentials_are_never_required_or_included(pool_file):
    path, _ = pool_file
    settings = load_config(str(path))
    settings = replace(settings, accounts=tuple(replace(account, enabled=False) for account in settings.accounts))
    assert valid_tag(snapshot_tag(settings, TOKEN, {}))
    with pytest.raises(ValueError):
        snapshot_tag(settings, TOKEN, KEYS)


@pytest.mark.parametrize("value", [None, 123, "", "v1:", "v2:" + "a" * 64, "v1:" + "A" * 64,
                                  "v1:" + "0" * 63, "v1:" + "0" * 65, "v1:" + "g" * 64,
                                  "v1:" + "0" * 64 + "\n", "v1:" + "0" * 64 + ",other"])
def test_untrusted_tags_require_exact_version_and_bounded_ascii_format(value):
    assert not valid_tag(value)


def test_snapshot_field_coverage_requires_review_when_config_changes():
    assert {field.name for field in fields(PoolConfig)} == {
        "pool_id", "accounts", "models", "token_file", "state_file", "port", "request_timeout",
        "probe_after", "max_connections", "usage_enabled", "usage_max_age", "usage_timeout"}

