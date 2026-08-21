"""Capability Grant authorization (device fabric D1/D3, task-level grants).

A CapabilityGrant binds a principal (agent id), node, actions, resources,
environment and expiry into a minimal bearer ticket that the Runner or
control plane validates before every execution.  Kept public through
``pbatch.capabilities`` for callers that import the grant APIs there; the
registry, trust matrix and CLI adapter stay in the parent module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from typing import Optional

# Side-effect and data classification axes shared with the registry.
EFFECT_CLASSES = ("pure", "read_only", "sandboxed", "reversible",
                  "compensatable", "irreversible")
EFFECT_ORDER = {name: index for index, name in enumerate(EFFECT_CLASSES)}
DATA_CLASSES = ("public", "internal", "confidential", "secret", "regulated")
DATA_CLASS_LEVEL = {name: index for index, name in enumerate(DATA_CLASSES)}


@dataclass(frozen=True)
class CapabilityGrant:
    """绑定主体、节点、动作、资源、环境和有效期的最小授权票据。

    设备织网额外使用 ``grant_id/device_id/task_node_id`` 与数据级别字段；
    它们全部带默认值，旧事务 grant 仍按原契约解析。
    """

    agent_id: str
    node_id: str
    actions: list
    resources: list
    environment: str
    expires_at: str
    max_side_effect_class: str
    capability: str = ""
    grant_id: str = ""
    task_node_id: str = ""
    device_id: str = ""
    allowed_data_classes: list = None
    issued_at: str = ""

    def to_dict(self) -> dict:
        return {"grant_id": self.grant_id, "agent_id": self.agent_id,
                "actions": list(self.actions), "resources": list(self.resources),
                "node_id": self.node_id, "task_node_id": self.task_node_id,
                "device_id": self.device_id,
                "environment": self.environment, "expires_at": self.expires_at,
                "issued_at": self.issued_at,
                "allowed_data_classes": list(self.allowed_data_classes or []),
                "max_side_effect_class": self.max_side_effect_class,
                "capability": self.capability}

    @classmethod
    def from_dict(cls, value: dict) -> "CapabilityGrant":
        if not isinstance(value, dict):
            raise ValueError("capability grant must be an object")
        aliases = {"agentId": "agent_id", "nodeId": "node_id",
                   "grantId": "grant_id", "taskNodeId": "task_node_id",
                   "deviceId": "device_id", "allowedActions": "actions",
                   "allowedResources": "resources",
                   "allowedDataClasses": "allowed_data_classes",
                   "issuedAt": "issued_at", "maxEffectClass": "max_effect_class",
                   "expiresAt": "expires_at",
                   "maxSideEffectClass": "max_side_effect_class"}
        normalized = {aliases.get(key, key): item for key, item in value.items()}
        allowed = {"agent_id", "node_id", "actions", "resources", "environment",
                   "expires_at", "max_side_effect_class", "max_effect_class",
                   "capability", "grant_id", "task_node_id", "device_id",
                   "allowed_data_classes", "issued_at"}
        unknown = sorted(set(normalized) - allowed)
        if unknown:
            raise ValueError("unknown grant fields: " + ", ".join(unknown))
        return cls(
            agent_id=str(normalized.get("agent_id", "")),
            node_id=str(normalized.get("node_id", "")),
            actions=list(normalized.get("actions", []) or []),
            resources=list(normalized.get("resources", []) or []),
            environment=str(normalized.get("environment", "")),
            expires_at=str(normalized.get("expires_at", "")),
            max_side_effect_class=str(
                normalized.get("max_side_effect_class",
                               normalized.get("max_effect_class", ""))),
            capability=str(normalized.get("capability", "")),
            grant_id=str(normalized.get("grant_id", "")),
            task_node_id=str(normalized.get("task_node_id", "")),
            device_id=str(normalized.get("device_id", "")),
            allowed_data_classes=list(
                normalized.get("allowed_data_classes", []) or []),
            issued_at=str(normalized.get("issued_at", "")),
        )


def _capability_record(value) -> dict:
    if isinstance(value, dict):
        record = dict(value)
        record.setdefault("id", "")
        return record
    identifier = str(value or "").strip()
    from .capabilities import build_registry  # deferred: parent re-exports us
    for item in build_registry():
        if item.get("id") == identifier:
            return item
    if identifier:
        # Transaction-required capabilities may be domain-specific strings
        # (e.g. approval.write) and need an explicit grant even if they are
        # not top-level CLI commands.
        return {"id": identifier, "domain": "execution", "risk": "medium"}
    return {}


def _parse_expiry(value: str) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _grant_identity_reasons(grant: CapabilityGrant, capability: dict,
                            agent_id: str, node_id: str, device_id: str = "",
                            task_node_id: str = "") -> list:
    reasons = []
    if not grant.agent_id or grant.agent_id != agent_id:
        reasons.append("agent_id 不匹配")
    if not grant.node_id or grant.node_id != node_id:
        reasons.append("node_id 不匹配")
    if grant.capability and grant.capability != capability.get("id"):
        reasons.append("grant capability 不匹配")
    if grant.device_id and (not device_id or grant.device_id != device_id):
        reasons.append("device_id 不匹配")
    if grant.task_node_id and (not task_node_id
                               or grant.task_node_id != task_node_id):
        reasons.append("task_node_id 不匹配")
    if not grant.actions or not grant.resources:
        reasons.append("grant 缺少 actions/resources")
    return reasons


def _grant_scope_reasons(grant: CapabilityGrant, action: str, resource: str,
                         environment: str, effect_class: str,
                         now: Optional[datetime], data_class: str) -> list:
    reasons = []
    if not any(fnmatchcase(str(action), str(pattern)) for pattern in grant.actions):
        reasons.append("action 不在 grant 范围")
    if not any(fnmatchcase(str(resource), str(pattern))
               for pattern in grant.resources):
        reasons.append("resource 不在 grant 范围")
    if not grant.environment or grant.environment != environment:
        reasons.append("environment 不匹配")
    reasons.extend(_grant_effect_reasons(grant, effect_class))
    reasons.extend(_grant_data_reasons(grant, data_class))
    reasons.extend(_grant_expiry_reasons(grant, now))
    return reasons


def _grant_effect_reasons(grant: CapabilityGrant, effect_class: str) -> list:
    if effect_class not in EFFECT_ORDER:
        return ["未知副作用等级"]
    if grant.max_side_effect_class not in EFFECT_ORDER:
        return ["grant 副作用上限无效"]
    if EFFECT_ORDER[effect_class] > EFFECT_ORDER[grant.max_side_effect_class]:
        return ["请求副作用超过 grant 上限"]
    return []


def _grant_data_reasons(grant: CapabilityGrant, data_class: str) -> list:
    if not grant.allowed_data_classes:
        return []
    if not data_class:
        return ["请求缺少数据级别"]
    allowed = any(fnmatchcase(str(data_class), str(pattern))
                  for pattern in grant.allowed_data_classes)
    return [] if allowed else ["数据级别不在 grant 范围"]


def _grant_expiry_reasons(grant: CapabilityGrant,
                          now: Optional[datetime]) -> list:
    try:
        expired = _parse_expiry(grant.expires_at) <= (
            now or datetime.now(timezone.utc))
    except (TypeError, ValueError):
        return ["grant expires_at 无效"]
    return ["grant 已过期"] if expired else []


def authorize_grant(capability, grant, agent_level: int, agent_id: str,
                    node_id: str, action: str, resource: str,
                    environment: str, effect_class: str = "pure",
                    device_zone: str = "", now: Optional[datetime] = None,
                    data_class: str = "", device_id: str = "",
                    task_node_id: str = "") -> tuple:
    """执行前校验一张 grant；返回 ``(allowed, reasons)``。"""
    from .capabilities import authorized  # deferred: parent re-exports us
    record = _capability_record(capability)
    if not record:
        return False, ["未知 capability"]
    if not isinstance(grant, CapabilityGrant):
        try:
            grant = CapabilityGrant.from_dict(grant)
        except (TypeError, ValueError) as exc:
            return False, [str(exc)]
    allowed, trust_reason = authorized(record, agent_level, device_zone)
    reasons = [] if allowed else [trust_reason]
    reasons.extend(_grant_identity_reasons(
        grant, record, agent_id, node_id, device_id, task_node_id))
    reasons.extend(_grant_scope_reasons(grant, action, resource, environment,
                                        effect_class, now, data_class))
    return not reasons, reasons


def authorize_required_capabilities(required: list, grants: list,
                                    context: Optional[dict] = None) -> tuple:
    """要求每项能力至少有一张匹配 grant；缺任何一项即拒绝。"""
    context = dict(context or {})
    failures = []
    for capability in required or []:
        matched = False
        last_reasons = ["没有可用 grant"]
        for grant in grants or []:
            matched, last_reasons = authorize_grant(
                capability, grant, int(context.get("agent_level", -1)),
                str(context.get("agent_id", "")), str(context.get("node_id", "")),
                str(context.get("action", capability)),
                str(context.get("resource", "*")),
                str(context.get("environment", "")),
                str(context.get("effect_class", "pure")),
                str(context.get("device_zone", "")), context.get("now"),
                str(context.get("data_class", "")),
                str(context.get("device_id", "")),
                str(context.get("task_node_id", "")))
            if matched:
                break
        if not matched:
            failures.append(f"{capability}: " + "; ".join(last_reasons))
    return not failures, failures