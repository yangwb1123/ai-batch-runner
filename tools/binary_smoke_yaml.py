"""Bounded declarative input checks for the packaged CLI; no model calls."""

import json
import os
from pathlib import Path
import sys


def alias_expansion() -> str:
    """Exceed the node budget, while keeping a broken reader's expansion small."""
    raw, previous = "seed: &seed {prompt: binary-yaml-private}\n", "seed"
    for number in range(16):
        name = f"m{number}"
        raw += f"{name}: &{name} {{<<: [*{previous}, *{previous}]}}\n"
        previous = name
    return raw + f"tasks: [*{previous}]\n"


def _prepare(workspace: Path, environment: dict) -> tuple[Path, dict, list[str]]:
    root = workspace.parent / "yaml-checks"
    directory = root / "workspace"
    directory.mkdir(parents=True)
    agent = root / "unexpected-agent.py"
    agent.write_text("from pathlib import Path\nPath(" + repr(str(root / "agent-started"))
                     + ").write_text('unexpected launch')\n", encoding="utf-8")
    config = {"agent": {"bin": sys.executable, "default_model": "",
                        "argument_flags": {"prompt": [str(agent)]},
                        "session_flags": {"start": [], "continue": [], "fork": []}}}
    (directory / "pi-batch.yaml").write_text(json.dumps(config), encoding="utf-8")
    environment = {**environment, "PBATCH_SCRIPT_DIR": str(directory),
                   "PBATCH_HOME": str(root / "home"), "PBATCH_RUNTIME_HOME": str(root / "runtime")}
    flags = ["--agent-bin", sys.executable, "--dry-run", "--memory-mode", "off", "--log-file", ""]
    return directory, environment, flags


def _validate(output: str, root: Path, expected: str) -> None:
    if expected not in output or "Traceback" in output or "binary-yaml-private" in output:
        raise RuntimeError("YAML input check lacked its bounded diagnostic or exposed input")
    if (root / "agent-started").exists():
        raise RuntimeError("YAML input check unexpectedly launched the provider")


def check_yaml_inputs(command: list[str], workspace: Path, environment: dict,
                      timeout: float, *, execute) -> None:
    directory, environment, flags = _prepare(workspace, environment)
    cases = (
        ("merged tasks", "- &base {prompt: binary-yaml}\n- {<<: *base}\n", [], 0, "Tasks: 2"),
        ("task alias budget", alias_expansion(), [], 1, "expanded node count"),
        ("pipeline alias budget", alias_expansion(), ["--pipeline"], 1, "expanded node count"),
        ("private syntax diagnostic", "tasks: [binary-yaml-private", [], 1, "invalid YAML"),
    )
    for index, (label, text, prefix, code, expected) in enumerate(cases):
        source = directory / f"input-{index}.yaml"
        source.write_text(text, encoding="utf-8")
        execute([*command, *prefix, str(source), *flags], directory, environment, timeout,
                "YAML " + label, expected=(code,), include_stderr=True,
                validate=lambda output, expected=expected: _validate(output, directory.parent, expected))
    _check_configuration_fifo(command, directory, environment, timeout, execute)


def _check_configuration_fifo(command, directory, environment, timeout, execute):
    if not hasattr(os, "mkfifo"):
        return
    source = directory / "fifo-config.yaml"
    os.mkfifo(source)
    environment = {**environment, "PBATCH_PROJECT_CONFIG": str(source)}
    cases = (("check", ["config", "check", "--json"], 1),
             ("doctor", ["doctor", "--json"], 1),
             ("startup", ["-p", "doctor", "--dry-run", "--memory-mode", "off"], 2))
    for label, arguments, code in cases:
        runtime = directory.parent / ("fifo-runtime-" + label)
        env = {**environment, "PBATCH_RUNTIME_HOME": str(runtime)}
        execute([*command, *arguments], directory, env, timeout,
                "configuration FIFO " + label, expected=(code,), include_stderr=True,
                validate=lambda output: _validate(output, directory.parent, "regular file"))
        if label == "startup" and runtime.exists():
            raise RuntimeError("invalid configuration created runtime data before rejection")
