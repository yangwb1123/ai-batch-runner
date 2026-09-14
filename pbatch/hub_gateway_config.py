"""Validated local configuration for the outbound Hub Gateway."""

from __future__ import annotations

import os
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
    heartbeat_interval: float = 15.0
    poll_interval: float = 1.0


def normalize_config(options: HubGatewayConfig) -> HubGatewayConfig:
    cwd = str(Path(options.cwd or os.getcwd()).expanduser().resolve())
    name = options.name.strip() or socket.gethostname()
    project = options.project_id.strip() or Path(cwd).name or "workspace"
    state_dir = (str(Path(options.state_dir).expanduser().resolve())
                 if options.state_dir else str(config.data_dir(cwd) / "hub-gateway"))
    if not 1 <= int(options.sessions) <= 8:
        raise ValueError("Gateway session count must be between 1 and 8")
    if not 1 <= float(options.heartbeat_interval) <= 30:
        raise ValueError("heartbeat interval must be between 1 and 30 seconds")
    if not 0.1 <= float(options.poll_interval) <= 30:
        raise ValueError("poll interval must be between 0.1 and 30 seconds")
    return HubGatewayConfig(
        hub_url=options.hub_url, cwd=cwd, state_dir=state_dir,
        instance_id=options.instance_id, name=name, project_id=project,
        agent=options.agent or config.AGENT_BIN, model=options.model,
        provider=options.provider, runtime=options.runtime or "auto",
        sessions=int(options.sessions), compute_tools=options.compute_tools,
        heartbeat_interval=float(options.heartbeat_interval),
        poll_interval=float(options.poll_interval))


def session_options(options: HubGatewayConfig, name: str) -> AgentSessionOptions:
    return AgentSessionOptions(
        cwd=options.cwd, agent=options.agent, model=options.model,
        provider=options.provider, runtime=options.runtime,
        timeout=config.AGENT_DEFAULT_TIMEOUT, session_name=name,
        workspace_id=options.project_id)


__all__ = ["HubGatewayConfig", "normalize_config", "session_options"]
