#!/usr/bin/env python3
"""Unit tests for the explicit project-config override (PBATCH_PROJECT_CONFIG).

The validator registry is project data. A tool checkout that drives campaigns
for other repositories must not carry a per-project copy of pi-batch.yaml, and
a target repository must not have to vendor a copy of the tool to get its own
validators. PBATCH_PROJECT_CONFIG points the loader at the project's file.

Two properties matter and are pinned here:

- the override wins over the discovery order (entry script dir, package dir,
  working directory), so the project's ``validators`` replace the tool's;
- a path that does not exist fails closed. Falling back to the tool's registry
  would silently run a campaign against validators the target repository never
  declared — the drift this override exists to prevent.
"""

import os
import tempfile
import unittest
from pathlib import Path

from pbatch import config as batch_config

PROJECT = """\
agent:
  bin: project-agent
validators:
  repo-gate:
    cmd: "make check"
"""


def _write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


class ProjectConfigOverrideTest(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get(batch_config.BATCH_CONFIG_ENV, "")
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved:
            os.environ[batch_config.BATCH_CONFIG_ENV] = self._saved
        else:
            os.environ.pop(batch_config.BATCH_CONFIG_ENV, None)

    def test_explicit_path_wins_over_discovery(self):
        project = _write(self.root, "project-pi-batch.yaml", PROJECT)
        # A competing file in the working directory must lose.
        _write(self.root, "pi-batch.yaml", "validators:\n  repo-gate:\n    cmd: \"make wrong\"\n")
        os.environ[batch_config.BATCH_CONFIG_ENV] = str(project)
        document = batch_config._load_config_document(batch_config._project_config_path())
        self.assertEqual(document["agent"]["bin"], "project-agent")
        self.assertEqual(document["validators"]["repo-gate"]["cmd"], "make check")

    def test_missing_explicit_path_is_not_silently_ignored(self):
        missing = self.root / "absent" / "pi-batch.yaml"
        _write(self.root, "pi-batch.yaml", "validators:\n  repo-gate:\n    cmd: \"make fallback\"\n")
        os.environ[batch_config.BATCH_CONFIG_ENV] = str(missing)
        resolved = batch_config._project_config_path()
        self.assertEqual(resolved, missing, "the bad path must be reported, not replaced")
        with self.assertRaises(ValueError) as raised:
            batch_config._load_config_document(resolved)
        self.assertIn("cannot read YAML", str(raised.exception))

    def test_startup_config_fails_closed_on_a_missing_override(self):
        os.environ[batch_config.BATCH_CONFIG_ENV] = str(self.root / "nope.yaml")
        # _startup_config refuses execution outright rather than returning a
        # merged document, so the refusal is the contract.
        with self.assertRaises(SystemExit) as raised:
            batch_config._startup_config()
        self.assertEqual(raised.exception.code, 2)

    def test_discovery_order_unchanged_without_the_variable(self):
        os.environ.pop(batch_config.BATCH_CONFIG_ENV, None)
        local = _write(self.root, "pi-batch.yaml", PROJECT)
        cwd = Path.cwd()
        os.chdir(self.root)
        try:
            self.assertEqual(batch_config._project_config_path().resolve(), local.resolve())
        finally:
            os.chdir(cwd)

    def test_blank_value_is_treated_as_unset(self):
        local = _write(self.root, "pi-batch.yaml", PROJECT)
        os.environ[batch_config.BATCH_CONFIG_ENV] = "   "
        cwd = Path.cwd()
        os.chdir(self.root)
        try:
            self.assertEqual(batch_config._project_config_path().resolve(), local.resolve())
        finally:
            os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
