"""Compatibility validation for governance-aware rule registries."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .rule_registry import check_registry as _validate_core

_LEVELS = ("invariant", "contract", "policy", "heuristic", "suggestion")
_TIERS = {"demo", "standard", "production"}
_EXTENSION_FIELDS = {"level", "suppress_on", "scope", "counter_signals",
                       "samples", "confidence"}


def _string_list_ok(value) -> bool:
    return (isinstance(value, list) and bool(value)
            and all(isinstance(item, str) and item.strip() for item in value))


def _list_field_errors(rule_id: str, rule: dict, signals: set) -> list:
    """counter_signals / samples 列表字段校验（含信号声明检查）。"""
    errors = []
    for key in ("counter_signals", "samples"):
        value = rule.get(key)
        if value is not None and not _string_list_ok(value):
            errors.append(f"rule '{rule_id}': {key} must be a non-empty "
                          f"string list")
        elif key == "counter_signals" and value is not None:
            unknown = sorted(set(value) - signals)
            if unknown:
                errors.append(f"rule '{rule_id}': counter_signals {unknown} "
                              f"not declared in signals section")
    return errors


def _rule_level_errors(rule_id: str, rule: dict, signals: set) -> list:
    """level / scope / counter_signals / samples / confidence 校验。"""
    errors = []
    level = rule.get("level")
    if level is not None and level not in _LEVELS:
        errors.append(
            f"rule '{rule_id}': invalid level {level!r} "
            f"(must be one of {_LEVELS})")
    scope = rule.get("scope")
    if scope is not None and (not isinstance(scope, str) or not scope.strip()):
        errors.append(f"rule '{rule_id}': scope must be a non-empty string")
    elif scope is not None and scope != "any" and scope not in signals:
        errors.append(f"rule '{rule_id}': scope '{scope}' must be 'any' or "
                      f"a declared signal ({sorted(signals) or 'none'})")
    errors.extend(_list_field_errors(rule_id, rule, signals))
    confidence = rule.get("confidence")
    if confidence is not None and (not isinstance(confidence, (int, float))
                                   or isinstance(confidence, bool)
                                   or not 0 <= confidence <= 1):
        errors.append(f"rule '{rule_id}': confidence must be a number "
                      f"in [0, 1]")
    return errors


def _extension_errors(registry: dict) -> list[str]:
    errors = []
    signals = set((registry.get("signals") or {}).keys())
    for rule_id, rule in (registry.get("rules") or {}).items():
        if not isinstance(rule, dict):
            continue
        errors.extend(_rule_level_errors(str(rule_id), rule, signals))
        errors.extend(_suppression_errors(str(rule_id), rule.get("suppress_on")))
    return errors


def _suppression_errors(rule_id: str, suppressions) -> list[str]:
    if suppressions is None:
        return []
    if (not isinstance(suppressions, list) or not suppressions
            or not all(isinstance(item, str) and item.strip()
                       for item in suppressions)):
        return [f"rule '{rule_id}': suppress_on must be a non-empty string list"]
    return [f"rule '{rule_id}': suppress_on '{signal}' must be a tier or high/low"
            for signal in suppressions
            if signal not in _TIERS and signal not in ("high", "low")]


def _rule_files(rule: dict) -> list:
    files = list(rule.get("files") or [])
    template = rule.get("files_template")
    if isinstance(template, str):
        files.extend(template.replace("{profile}", str(profile))
                     for profile in rule.get("profiles", []) or [])
    return files


def _resolved_candidates(value, asset_base: Optional[Path]) -> set[str]:
    path = Path(str(value))
    candidates = {str(path.resolve())}
    if not path.is_absolute() and asset_base is not None:
        candidates.add(str((asset_base / path).resolve()))
    return candidates


def _self_reference_errors(registry: dict, registry_paths: list,
                           asset_base: Optional[Path]) -> list[str]:
    resolved = {str(Path(path).resolve()) for path in registry_paths}
    errors = []
    for rule_id, rule in (registry.get("rules") or {}).items():
        if not isinstance(rule, dict):
            continue
        for value in _rule_files(rule):
            if _resolved_candidates(value, asset_base) & resolved:
                errors.append(
                    f"rule '{rule_id}': files entry '{value}' resolves to the "
                    "registry file itself (self-referential rule)")
    return errors


def _without_extensions(registry: dict) -> dict:
    normalized = dict(registry)
    rules = registry.get("rules")
    if isinstance(rules, dict):
        normalized["rules"] = {
            rule_id: ({key: value for key, value in rule.items()
                       if key not in _EXTENSION_FIELDS}
                      if isinstance(rule, dict) else rule)
            for rule_id, rule in rules.items()
        }
    return normalized


def validate_registry(registry: dict, asset_base: Optional[Path] = None,
                      registry_paths: Optional[list] = None) -> list[str]:
    """Validate core schema, governance extensions and self references."""
    if registry_paths is None and isinstance(asset_base, (list, tuple, set)):
        registry_paths, asset_base = list(asset_base), None
    if not isinstance(registry, dict):
        return _validate_core(registry, asset_base)
    errors = _validate_core(_without_extensions(registry), asset_base)
    errors.extend(_extension_errors(registry))
    if registry_paths:
        errors.extend(_self_reference_errors(
            registry, list(registry_paths), asset_base))
    return errors
