"""Read-only local setup diagnostics; never attest provider quota or authentication."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
from pathlib import Path

from .llm_pool_config import config_path, parse_config
from .llm_pool_config_io import read_document
from .llm_pool_secrets import read_secret
from .llm_pool_snapshot import SNAPSHOT_HEADER, snapshot_tag, valid_tag
from .llm_pool_transport import UpstreamRequest


GATEWAY_TIMEOUT = 3
MAX_MODELS_BYTES = 65536
_SNAPSHOT_SCOPES = ("gateway_account_snapshot", "gateway_protocol_mapping",
                    "gateway_credential_snapshot", "gateway_runtime_configuration")
_NOT_CHECKED = ["provider_authentication", "provider_quota", "runtime_state",
                *_SNAPSHOT_SCOPES, "client_configuration"]


def add_doctor_command(commands):
    parser = commands.add_parser("doctor", help="逐项诊断本地接入；不查询供应商、不修改状态")
    parser.add_argument("--gateway", action="store_true", help="显式核验本机网关令牌、模型和已加载配置")
    parser.add_argument("--json", action="store_true", help="输出脱敏结构化报告")


def _check(name, status, code, hint="", **metadata):
    return {"check": name, "status": status, "code": code, "hint": hint, **metadata}


def _credential(path, name, **metadata):
    try:
        value = read_secret(path)
    except (OSError, ValueError):
        return None, _check(name, "fail", "credential_unavailable",
                            "检查文件存在、当前用户所有、目录 0700/文件 0600、非链接及内容格式；"
                            "账号用 pool key-set 导入，网关令牌用 pool token-init 创建。", **metadata)
    return value, _check(name, "pass", "private_file_valid", **metadata)


def _credentials(settings, checks):
    token, result = _credential(settings.token_file, "gateway_token")
    checks.append(result)
    groups, conflicts, complete, keys = {}, [], True, {}
    for account in settings.accounts:
        if not account.enabled:
            checks.append(_check("account_credential", "skip", "account_disabled",
                                 account=account.id))
            continue
        key, result = _credential(Path(account.credential_file), "account_credential",
                                  account=account.id)
        checks.append(result)
        if key is None:
            complete = False
            continue
        keys[account.id] = key
        if token is not None and hmac.compare_digest(key, token):
            result.update(status="fail", code="gateway_token_reused",
                          hint="网关令牌不可复用供应商 Key；显式轮换令牌后重启网关并更新客户端。")
        digest = hashlib.sha256(key.encode("ascii")).hexdigest()
        if digest in groups and groups[digest] != account.quota_group:
            conflicts.append(account.id)
        groups[digest] = account.quota_group
    checks.append(_check("credential_groups", "fail" if conflicts else "pass" if complete else "skip",
                         "credential_group_conflict" if conflicts else
                         "credential_groups_valid" if complete else "credentials_incomplete",
                         "相同 Key 必须声明为同一配额组；不从 Key 推断服务端账号归属。",
                         accounts=conflicts))
    if any(item["code"] == "gateway_token_reused" for item in checks):
        token = None
    return token, keys if complete else None


def _models_payload(response):
    lengths = response.headers.get_all("Content-Length", [])
    if (len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit()
            or len(lengths[0]) > 5 or not 1 <= int(lengths[0]) <= MAX_MODELS_BYTES
            or response.headers.get("Transfer-Encoding") is not None
            or len(response.headers.get_all("Content-Type", [])) != 1
            or len(response.headers.get_all("Content-Encoding", [])) > 1
            or response.headers.get("Content-Encoding", "identity") != "identity"
            or response.headers.get_content_type() != "application/json"):
        raise ValueError("invalid models framing")
    raw = response.read(MAX_MODELS_BYTES + 1)
    if len(raw) != int(lengths[0]):
        raise ValueError("incomplete models response")
    from .llm_pool_gateway import _invalid_constant, _unique_object
    return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)


def _models_match(data, models):
    if not isinstance(data, dict) or data.get("object") != "list":
        raise ValueError("invalid models list")
    entries = data.get("data")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 128:
        raise ValueError("invalid models entries")
    names = []
    for entry in entries:
        if (not isinstance(entry, dict) or entry.get("object") != "model"
                or entry.get("owned_by") != "opencode-go" or not isinstance(entry.get("id"), str)):
            raise ValueError("invalid model entry")
        names.append(entry["id"])
    if len(set(names)) != len(names):
        raise ValueError("duplicate models")
    return set(names) == set(models)


def _snapshot_result(settings, token, keys, headers):
    if keys is None:
        return _check("gateway_snapshot", "skip", "gateway_snapshot_local_incomplete",
                      "先修复本地启用账号的凭据，再核验网关加载状态。")
    values = headers.get_all(SNAPSHOT_HEADER, [])
    if not values:
        return _check("gateway_snapshot", "fail", "gateway_snapshot_unavailable",
                      "端点未提供配置校验信息；升级并重启账号池网关，不能仅凭模型清单确认接入。")
    if len(values) != 1 or not valid_tag(values[0]):
        return _check("gateway_snapshot", "fail", "gateway_snapshot_invalid",
                      "网关配置校验信息格式不受支持；检查端口和版本，不采信原始响应。")
    matches = hmac.compare_digest(values[0], snapshot_tag(settings, token, keys))
    return _check("gateway_snapshot", "pass" if matches else "fail",
                  "gateway_snapshot_match" if matches else "gateway_snapshot_mismatch",
                  "网关启动时的配置与启用凭据和本次本地读取一致；不代表供应商认证或额度有效。" if matches else
                  "网关加载状态与本次本地读取不一致；核对配置及运行目录，变更或轮换凭据后重启网关。")


def _gateway_response(settings, token, keys, response):
    if response.status != 200:
        code = ("gateway_auth_rejected" if response.status in (401, 403) else
                "gateway_redirect_rejected" if 300 <= response.status < 400 else "gateway_http_error")
        return [_check("gateway", "fail", code,
                       "检查端口、令牌和网关进程；轮换令牌后需重启。不跟随重定向。")]
    matches = _models_match(_models_payload(response), settings.models)
    result = _check("gateway", "pass" if matches else "fail",
                    "gateway_models_match" if matches else "gateway_models_mismatch",
                    "端点接受当前令牌且模型 ID 一致；加载状态由 gateway_snapshot 单独核验。" if matches else
                    "模型清单与磁盘配置不一致；检查端口及配置，必要时重启网关。")
    return [result, _snapshot_result(settings, token, keys, response.headers)]


def _gateway(settings, token, keys):
    if token is None:
        return [_check("gateway", "skip", "gateway_token_unavailable",
                       "先修复本地网关令牌，再使用 --gateway 检查。")]
    origin = f"http://127.0.0.1:{settings.port}/v1"
    try:
        with UpstreamRequest(origin, GATEWAY_TIMEOUT) as request:
            with request.models(token) as response:
                result = _gateway_response(settings, token, keys, response)
                request.remaining()
    except (OSError, http.client.HTTPException):
        return [_check("gateway", "fail", "gateway_unavailable",
                       "确认 pool serve 已启动、端口一致且未繁忙；探测总时限为 3 秒。")]
    except (ValueError, RecursionError):
        return [_check("gateway", "fail", "gateway_invalid_response",
                       "端点未返回合法的有界模型清单；检查端口是否对应账号池网关。")]
    return result


def doctor_report(path="", *, gateway=False):
    checks = []
    report = {"schema_version": 1, "scope": "local_setup", "ok": False,
              "read_only": True, "pool_id": None, "checks": checks,
              "not_checked": list(_NOT_CHECKED)}
    if not gateway:
        report["not_checked"].append("gateway")
    try:
        source = config_path(path)
        data, _, _ = read_document(source)
        settings = parse_config(data, source.parent)
    except (OSError, ValueError):
        checks.append(_check("configuration", "fail", "configuration_unavailable",
                             "检查配置路径、YAML schema、当前用户所有及非链接文件；"
                             "配置与父目录不可由组/其他用户写入。缺少配置时使用 pool init。"))
        if gateway:
            checks.append(_check("gateway", "skip", "configuration_unavailable"))
            report["not_checked"].append("gateway")
        return report
    report["pool_id"] = settings.pool_id
    checks.append(_check("configuration", "pass", "configuration_valid"))
    enabled = sum(account.enabled for account in settings.accounts)
    checks.append(_check("enabled_accounts", "pass" if enabled else "fail",
                         "accounts_enabled" if enabled else "no_enabled_accounts",
                         "至少启用一个配置账号；此项不检查运行态暂停、认证故障或额度。",
                         count=enabled))
    token, keys = _credentials(settings, checks)
    if gateway:
        results = _gateway(settings, token, keys)
        checks.extend(results)
        if results[0]["status"] == "skip":
            report["not_checked"].append("gateway")
        if any(item["code"] in {"gateway_snapshot_match", "gateway_snapshot_mismatch"} for item in results):
            report["not_checked"] = [name for name in report["not_checked"] if name not in _SNAPSHOT_SCOPES]
    report["ok"] = all(item["status"] != "fail" for item in checks)
    return report


def doctor_main(path="", *, gateway=False, json_output=False):
    report = doctor_report(path, gateway=gateway)
    if json_output:
        print(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2))
    else:
        print("账号池本地接入检查：" + ("通过" if report["ok"] else "未通过"))
        for item in report["checks"]:
            names = [item["account"]] if "account" in item else item.get("accounts", [])
            account = " / " + ", ".join(names) if names else ""
            print(f"[{item['status'].upper()}] {item['check']}{account}: {item['code']}")
            if item["hint"]:
                print("  " + item["hint"])
        print("未检查：" + ", ".join(report["not_checked"]))
        print("只读诊断，不调用模型；通过不代表供应商认证有效、额度可用或客户端已接入。")
    return 0 if report["ok"] else 1
