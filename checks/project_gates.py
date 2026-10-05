"""Every project's campaign must validate against the gate that project declares.

The batch runner drives campaigns for repositories it does not own, so the
question "was this LLM-written change accepted?" has exactly one honest answer:
the target repository's own gate, run in the target repository. That is only
true if the campaign's ``validate`` field names a validator the project
declares, and that validator calls the target's documented gate.

This check is the mechanical form of that promise:

1. every ``projects/<name>/`` that has a campaign or pipeline declares a
   ``repo-gate`` validator (the one name a campaign may use);
2. every ``validate:`` field in a project's campaign or pipeline refers to a
   validator that project declares — a name from the tool's global registry is
   a drift risk, because the tool's list is not the target's AGENTS.md;
3. the project's validator is reachable the way the runner actually resolves
   it: PBATCH_PROJECT_CONFIG (see pbatch/config.py) must be what the project's
   run script exports, otherwise the tool's own registry wins and the whole
   arrangement is decorative.

A project may still declare extra validators for non-implementation stages
(artifact checks). The rule is that no stage may gate acceptance on one.
"""

import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - the gates degrade to a clear error
    yaml = None

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = ROOT / "projects"
GATE = "repo-gate"
CONFIG_ENV = "PBATCH_PROJECT_CONFIG"

# validate: gofmt, gotest      /  validate: |
#     engineering               /  validate:
#       - name
VALUE = re.compile(r"^\s*(?P<key>validate|validate_cmd)\s*:\s*(?P<rest>.*)$")


def _documents(directory: Path) -> list[Path]:
    return sorted(
        p for p in directory.glob("*.yaml")
        if p.name not in {"pi-batch.yaml"} and "campaign" in p.name or "pipeline" in p.name
    )


def _validators(project: Path) -> set[str]:
    config = project / "pi-batch.yaml"
    if not config.exists() or yaml is None:
        return set()
    document = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    registry = document.get("validators") or {}
    return set(registry) if isinstance(registry, dict) else set()


def _referenced(directory: Path) -> list[tuple[Path, str]]:
    """(file, validator name) for every scalar validate field in a project."""
    found: list[tuple[Path, str]] = []
    for path in _documents(directory):
        for line in path.read_text(encoding="utf-8").splitlines():
            match = VALUE.match(line)
            if not match:
                continue
            for name in re.split(r"[,\s]+", match.group("rest").strip()):
                name = name.strip()
                if name and not name.startswith(("#", "|", ">", "-", "{")):
                    found.append((path, name))
    return found


def _entrypoint(project: Path) -> Path | None:
    """The script that runs this project's campaigns.

    A project may own its entrypoint, or share one under scripts/<project>/ —
    sverp drives two targets from a single scripts/sverp/run-campaign.sh.
    """
    for candidate in (
        project / "run-campaign.sh",
        ROOT / "scripts" / project.name / "run-campaign.sh",
    ):
        if candidate.exists():
            return candidate
    return None


DOCS = ("AGENTS.md", "HARNESS.md", "CONTRIBUTING.md", "README.md")


def _target_repo(project: Path) -> Path | None:
    """Where the target repository lives, as declared by the project."""
    config = project / "pi-batch.yaml"
    if not config.exists() or yaml is None:
        return None
    document = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    declared = (document.get("project") or {}).get("target_repo")
    return Path(declared).expanduser() if declared else None


def _gate_command(project: Path) -> str | None:
    config = project / "pi-batch.yaml"
    if not config.exists() or yaml is None:
        return None
    document = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    entry = (document.get("validators") or {}).get(GATE) or {}
    command = entry.get("cmd") if isinstance(entry, dict) else None
    return command


def _documented(repo: Path, command: str) -> list[str]:
    """Gate invocations the repository's own documents do not mention.

    `python3 cli.py quality` in a repository that documents `make ci` means
    the project is gating on something its AGENTS.md never asked for, and no
    amount of wiring makes that honest. The comparison is textual on purpose:
    the target repository owns the wording, and this check only asks that the
    command we call is the command it documents.
    """
    text = ""
    for name in DOCS:
        path = repo / name
        if path.is_file():
            text += path.read_text(encoding="utf-8", errors="replace")
    if not text:
        return []
    tokens = command.split()
    invocations: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "make" and index + 1 < len(tokens):
            invocations.append(f"make {tokens[index + 1]}")
            index += 2
            continue
        if token.endswith(".py") or token in {"pnpm", "cargo", "bash"}:
            invocations.append(" ".join(tokens[index:index + 2]))
            index += 2
            continue
        index += 1
    return [call for call in invocations if call not in text]


def _validator_failures(project: Path, referenced: list[tuple[Path, str]]) -> list[str]:
    failures = []
    declared = _validators(project)
    if GATE not in declared:
        failures.append(f"{project.name}: no {GATE} validator in pi-batch.yaml")
    for path, name in referenced:
        if name != GATE and name not in declared:
            failures.append(
                f"{project.name}: {path.name} validates on '{name}', which the project "
                f"does not declare (only {GATE} may gate acceptance)"
            )
    return failures


def _entrypoint_failures(project: Path) -> list[str]:
    run_script = _entrypoint(project)
    if run_script is None:
        return [f"{project.name}: no run-campaign.sh to export {CONFIG_ENV}"]
    if CONFIG_ENV in run_script.read_text(encoding="utf-8"):
        return []
    return [
        f"{project.name}: {run_script.relative_to(ROOT)} does not export {CONFIG_ENV}, "
        "so the tool's own registry would be used instead of the project's"
    ]


def _documentation_failures(project: Path) -> tuple[list[str], list[str]]:
    """(failures, skipped) for the cross-check against the target repository.

    An absent sibling checkout is not a contract violation of *this* repository:
    it means the cross-check could not be evaluated here, so it is reported as a
    skip rather than silently passed or turned into a red build. Every other
    check below reads only files inside this repository and stays fail-closed.
    """
    command, repo = _gate_command(project), _target_repo(project)
    if not command:
        return [], []
    if not repo:
        return [f"{project.name}: pi-batch.yaml does not declare project.target_repo"], []
    if not repo.is_dir():
        return [], [f"{project.name}: target repo {repo} not present, cross-check not evaluated"]
    undocumented = [
        f"{project.name}: repo-gate calls `{call}`, which the repository's documents "
        f"({'/'.join(DOCS)}) never state"
        for call in _documented(repo, command)
    ]
    return undocumented, []


def _inspect_project(project: Path) -> tuple[bool, list[str], list[str]]:
    referenced = _referenced(project)
    if not referenced:
        return False, [], []
    failures = _validator_failures(project, referenced)
    failures.extend(_entrypoint_failures(project))
    documentation, skipped = _documentation_failures(project)
    failures.extend(documentation)
    return True, failures, skipped


def _projects() -> list[Path]:
    if not PROJECTS.is_dir():
        return []
    return sorted(path for path in PROJECTS.iterdir() if path.is_dir())


def _report(failures: list[str], skipped: list[str], checked: int) -> int:
    for skip in skipped:
        print(f"SKIP: project gates — {skip}")
    for failure in failures:
        print(f"FAIL: project gates — {failure}")
    if failures:
        return 1
    summary = f"PASS: project gates ({checked} campaign project(s) validate on their own {GATE}"
    summary += f", {len(skipped)} cross-check skipped)" if skipped else ")"
    print(summary)
    return 0


def run() -> int:
    if yaml is None:
        print("FAIL: project gates (PyYAML unavailable)")
        return 1
    failures: list[str] = []
    skipped: list[str] = []
    checked = 0
    for project in _projects():
        has_campaign, project_failures, project_skips = _inspect_project(project)
        checked += int(has_campaign)
        failures.extend(project_failures)
        skipped.extend(project_skips)
    return _report(failures, skipped, checked)


if __name__ == "__main__":
    sys.exit(run())
