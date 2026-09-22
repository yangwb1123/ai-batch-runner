"""Installed assets remain discoverable when metadata filters moved paths."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from pbatch import assets


@pytest.mark.parametrize("missing", [None, "scripts", "ui-specs", "backend-specs",
                                    "examples", "evals", "pi-batch.yaml"])
def test_target_install_assets_with_filtered_record_paths(tmp_path, monkeypatch, missing):
    package_root = tmp_path / "installed"
    runtime = package_root / "share" / "ai-batch-runner"
    runtime.mkdir(parents=True)
    if missing != "pi-batch.yaml":
        (runtime / "pi-batch.yaml").write_text("validators: {}\n", encoding="utf-8")
    for marker in ("scripts", "ui-specs", "backend-specs", "examples", "evals"):
        if marker != missing:
            (runtime / marker).mkdir()
    # pip --target moves data-files but retains their old relative RECORD paths;
    # metadata implementations may filter those paths because they no longer exist.
    distribution = SimpleNamespace(files=[Path("pbatch/assets.py")])
    monkeypatch.setattr(assets, "distribution", lambda _name: distribution)
    monkeypatch.setattr(assets, "__file__", str(package_root / "pbatch" / "assets.py"))
    monkeypatch.setattr(assets.sys, "prefix", str(tmp_path / "prefix"))
    monkeypatch.delattr(assets.sys, "_MEIPASS", raising=False)
    monkeypatch.delenv("PBATCH_TOOL_ROOT", raising=False)

    expected = runtime if missing is None else package_root
    assert assets._discover_tool_root() == expected
