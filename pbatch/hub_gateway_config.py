"""Validated local configuration for the outbound Hub Gateway."""

from __future__ import annotations

import os
import math
import socket
from dataclasses import dataclass
from pathlib import Path

from . import config
from .agent_session import AgentSessionOptions


@dataclass(frozen=True)
class HubGatewayConfig:
    hub_url: str
    cwd: str = ""
    state_dir: str = ""
    instance_id: str = ""
    name: str = ""
    project_id: str = ""
    agent: str = ""
    model: str = ""
    provider: str = ""
    runtime: str = "auto"
    compute_tools: bool = False
    sessions: int = 1
    max_sessions: int = 8
    heartbeat_interval: float = 15.0
    poll_interval: float = 1.0
    observe_only: bool = False
    publish_command_output: bool = False
    share_files: tuple[str, ...] = ()


def normalize_config(options: HubGatewayConfig) -> HubGatewayConfig:
    cwd = str(Path(options.cwd or os.getcwd()).expanduser().resolve())
    name = options.name.strip() or socket.gethostname()
    project = options.project_id.strip() or Path(cwd).name or "workspace"
    state_dir = (str(Path(options.state_dir).expanduser().resolve())
                 if options.state_dir else str(config.data_dir(cwd) / "hub-gateway"))
    if type(options.observe_only) is not bool:
        raise ValueError("Gateway observe_only must be a boolean")
    if type(options.publish_command_output) is not bool:
        raise ValueError("Gateway publish_command_output must be a boolean")
    from .hub_file_snapshot_models import validate_snapshot_paths
    if not isinstance(options.share_files, (tuple, list)):
        raise ValueError("Gateway share_files must be a list of exact relative paths")
    share_files = tuple(validate_snapshot_paths(list(options.share_files)))
    if options.observe_only:
        _observation_options(options, cwd, state_dir)
    minimum = 0 if options.observe_only else 1
    if type(options.max_sessions) is not int or not minimum <= options.max_sessions <= 8:
        raise ValueError("Gateway maximum session count is outside the selected mode")
    if type(options.sessions) is not int or not 0 <= options.sessions <= options.max_sessions:
        raise ValueError("Gateway session count must be between 0 and max_sessions")
    heartbeat = _interval(options.heartbeat_interval, 1, "heartbeat")
    poll = _interval(options.poll_interval, 0.1, "poll")
    return HubGatewayConfig(
        hub_url=options.hub_url, cwd=cwd, state_dir=state_dir,
        instance_id=options.instance_id, name=name, project_id=project,
        agent=options.agent or config.AGENT_BIN, model=options.model,
        provider=options.provider, runtime=options.runtime or "auto",
        sessions=options.sessions, max_sessions=options.max_sessions,
        compute_tools=options.compute_tools, observe_only=options.observe_only,
        publish_command_output=options.publish_command_output,
        share_files=share_files,
        heartbeat_interval=heartbeat, poll_interval=poll)


def _interval(value, minimum, label):
    if (type(value) not in (int, float) or not minimum <= value <= 30
            or not math.isfinite(value)):
        raise ValueError(f"{label} interval must be finite and between {minimum} and 30 seconds")
    return float(value)


def _observation_options(options, cwd, state_dir):
    from .hub_client import validate_hub_url
    from .hub_store_helpers import identifier
    if any(not isinstance(value, str) or not value.strip() for value in (
            options.cwd, options.state_dir, options.instance_id, options.project_id, options.hub_url)):
        raise ValueError("observation requires explicit Hub, workspace, state, instance and project")
    validate_hub_url(options.hub_url)
    identifier(options.instance_id, "instance_id")
    identifier(options.project_id, "project_id")
    if options.sessions != 0 or options.max_sessions != 0 or options.compute_tools is not False:
        raise ValueError("observation requires zero sessions/capacity and no compute tools")
    if options.publish_command_output:
        raise ValueError("observation cannot publish command output")
    if options.share_files:
        raise ValueError("observation cannot share workspace files")
    workspace, state = Path(cwd), Path(state_dir)
    if not workspace.is_dir():
        raise ValueError("observation requires an existing fixed workspace")
    if state == workspace or workspace in state.parents:
        raise ValueError("observation state must be outside the workspace")


def session_options(options: HubGatewayConfig, name: str, *, session_id: str = "") -> AgentSessionOptions:
    return AgentSessionOptions(
        cwd=options.cwd, agent=options.agent, model=options.model,
        provider=options.provider, runtime=options.runtime,
        timeout=config.AGENT_DEFAULT_TIMEOUT, session_name=name,
        workspace_id=options.project_id, session_id=session_id)


__all__ = ["HubGatewayConfig", "normalize_config", "session_options"]
