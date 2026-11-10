"""Public abr identity must not rename existing configuration or runtime data."""

import ast
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from pbatch import assets, cli, product_cli, version


ROOT = Path(__file__).resolve().parents[1]


def test_public_parser_and_version_use_abr(capsys):
    parser = cli.build_parser()
    assert parser.prog == "abr"
    with pytest.raises(SystemExit) as result:
        parser.parse_args(["--version"])
    assert result.value.code == 0
    assert capsys.readouterr().out.strip() == f"abr {version.VERSION}"
    assert product_cli.PRODUCT == "abr"


def test_installed_version_queries_distribution_metadata(monkeypatch):
    queried = []

    def metadata(name):
        queried.append(name)
        return "1.2.3"

    monkeypatch.setattr(version, "metadata_version", metadata)
    assert version._package_version() == "1.2.3"
    assert queried == ["ai-batch-runner"]


def test_asset_metadata_uses_distribution_without_migrating_asset_paths(tmp_path, monkeypatch):
    queried = []

    def distribution(name):
        queried.append(name)
        return SimpleNamespace(files=[])

    monkeypatch.setattr(assets, "__file__", str(tmp_path / "pbatch/assets.py"))
    monkeypatch.setattr(assets, "distribution", distribution)
    assert assets._distribution_asset_root() is None
    assert queried == ["ai-batch-runner"]


@pytest.mark.parametrize("module, extra", [("jwt", "hub"), ("snaplink_sso", "web")])
def test_optional_dependency_errors_name_installable_distribution(monkeypatch, module, extra):
    from pbatch.hub_jwks import SnaplinkVerifier
    from pbatch.hub_web import HubWeb

    settings = {
        "web": {"public_origin": "https://abr.test", "client_id": "browser", "tenant_id": "tenant"},
        "allowed_origins": ["https://abr.test"],
        "auth": {"mode": "jwt", "issuer": "https://issuer.test", "jwks_url": "https://issuer.test/jwks"},
    }
    monkeypatch.setitem(sys.modules, module, None)
    with pytest.raises(ValueError) as result:
        if extra == "hub":
            SnaplinkVerifier(settings["auth"])
        else:
            HubWeb(settings, SimpleNamespace(mode="jwt"))
    assert f'pip install "ai-batch-runner[{extra}]"' in str(result.value)


@pytest.mark.parametrize("shell, registration", [
    ("bash", "complete -F _abr_complete abr pi-batch"),
    ("zsh", "compdef _abr abr pi-batch"),
])
def test_completion_registers_canonical_command_and_compatibility_alias(shell, registration):
    script = product_cli._completion_script(shell)
    assert registration in script
    assert "doctor" in script and "tui" in script


def test_subcommand_parsers_advertise_canonical_command():
    legacy = []
    for path in (ROOT / "pbatch").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.keyword) and node.arg == "prog"
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                    and node.value.value.startswith("pi-batch")):
                legacy.append(f"{path.name}:{node.lineno}")
    assert not legacy, legacy
