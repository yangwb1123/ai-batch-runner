"""Development bootstrap makes private, unique, non-overwriting credentials."""

import json
import stat

import pytest

from pbatch.hub_auth import HubAuth
from pbatch.hub_setup import create_development_config


def test_private_distinct_credentials_and_no_overwrite(tmp_path):
    target = tmp_path / "dev"
    result = create_development_config(target)
    auth = HubAuth(json.loads((target / "hub.json").read_text())["auth"])
    user_token = (target / "user.token").read_text().strip()
    user = auth.authenticate("Bearer " + user_token)
    assert user.kind == "user" and user.instances == {"alpha", "beta"}
    assert user_token not in json.dumps(result)
    for name in ("alpha", "beta"):
        token = (target / f"gateway-{name}.token").read_text().strip()
        principal = auth.authenticate("Bearer " + token)
        assert principal.kind == "gateway" and principal.instances == {name}
        assert token != user_token
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in target.iterdir())
    with pytest.raises(ValueError, match="already exist"):
        create_development_config(target)
    assert (target / "user.token").read_text().strip() == user_token


@pytest.mark.parametrize("instances", [[], ["alpha", "alpha"], ["../../escape"], ["name/other"]])
def test_invalid_names_do_not_create_files(tmp_path, instances):
    target = tmp_path / "dev"
    with pytest.raises(ValueError):
        create_development_config(target, instances)
    assert not target.exists()


def test_symlink_directory_refused(tmp_path):
    target = tmp_path / "dev"
    target.symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError):
        create_development_config(target)
    assert not (tmp_path / "missing").exists()
