# 平台测试手册 — 192.168.123.52

> 一条命令验证全部：`bash /home/ywb/test-platform.sh {status|audit|e2e}`
> 凭据文件 `/home/ywb/.platform-test/keys.txt`（0600，测试专用）。

## 1. 快速健康检查（无需凭据）

```bash
bash /home/ywb/test-platform.sh status
# 8 个 systemd 服务 + 11 个端口（13013-13023）+ 健康端点 + audit 401 门禁
```

## 2. 审计链路 API 测试（自动刷新凭据）

```bash
bash /home/ywb/test-platform.sh audit
# 三分支语义：202 accepted / 重投不冲突 / 篡改 409 / 缺字段 400
# 账本查询验证（sverp-web 来源事件存在）
```

## 3. 完整业务链路 E2E（真实 UI 等价流程）

```bash
bash /home/ywb/test-platform.sh e2e
# ① 登录：ERP 发起 → Snaplink 认证（admin）→ callback 建会话
# ② 创建质检单（POST /api/quality/inspections + CSRF）
# ③ 审计链路：outbox 落库 → 投递 published → 账本含 admin 事件
```

## 4. 手工 curl（排查用）

```bash
# 服务健康
curl -s http://127.0.0.1:13019/healthz            # aero-vault
curl -s http://127.0.0.1:13020/readyz             # aero-id
curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:13018/api/v1/events  # audit → 401

# 账本查询（审计事件是否入账）
bash /home/ywb/sync-sso-key.sh                    # 先同步公钥（snaplink 重启会轮换）
POLICY=$(curl -s -X POST http://127.0.0.1:13015/token \
  -d "grant_type=client_credentials&client_id=sso-admin&client_secret=$(sed -n 1p /home/ywb/.platform-test/keys.txt)&scope=audit:policy:write audit:platform:cross_tenant" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
curl -s "http://127.0.0.1:13018/api/v1/events?tenant_id=sverp&source_system=sverp-web&from=2026-08-06T00:00:00Z&to=2026-08-07T00:00:00Z" \
  -H "Authorization: Bearer $POLICY" | python3 -m json.tool | head -30
```

## 4.1 测试账号

| 账号 | 密码 | 权限 | 用途 |
|---|---|---|---|
| `admin` | `Admin-075b62a730d76a59` | sso-admin（SSO 管理）+ sverp-web admin（全权限） | 管理端 + 全功能测试 |
| `tester` | 见 `/home/ywb/.platform-test/accounts.txt` | **质检员最小权限**（quality:inspection:read/write、media:read/write、aps:schedule:read） | 日常功能测试 |

权限边界已验证：tester 可访问质检列表/创建质检单（200/201），系统用户/路由/内容管理全部 403。
创建质检单同样产生审计事件（outbox published → governance 账本）。

## 5. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| audit 投递 401 | snaplink 重启轮换了签名密钥 | `bash /home/ywb/sync-sso-key.sh`（timer 每 2 分钟自动同步；sverpweb 侧 401 自动刷新 token 重试） |
| e2e 登录失败 | admin 密码已轮换 | 更新 keys.txt 第 2 行 |
| outbox 大量 failed | 查看 `last_error` 列分类 | `[permanent]`=终态（配置/schema 问题）；其他=retryable 自动退避 |
| 测试脚本报 token 失败 | keys.txt 凭据过期 | 重新换 sso-admin/sverp-audit secret 并更新 keys.txt（3 行） |

## 6. 测试资产位置

| 资产 | 路径 |
|---|---|
| 测试脚本 | `/home/ywb/test-platform.sh` |
| 测试凭据 | `/home/ywb/.platform-test/keys.txt`（line1=sso-admin secret, line2=admin 密码, line3=sverp-audit secret） |
| 公钥同步 | `/home/ywb/sync-sso-key.sh` + `sync-sso-key.timer`（每 2 分钟） |
| outbox 检查 | `python3 /tmp/check-outbox.py` |

## 7. 平台集成测试页（sverpweb-stable 内置）

登录 ERP 后，系统管理 → **平台集成测试**（4 个页面，admin 可见）：

| 页面 | 路径 | 验证内容 |
|---|---|---|
| 集成总览 | `/integrations/status` | 三服务健康 + 配置状态 |
| 文件存储 (Vault) | `/integrations/vault` | BFF 代理上传/列表（sverp-vault 服务账号自动换 token） |
| 消息 (Aero IM) | `/integrations/im` | 发消息到 "SVERP Integration" 频道 + 读回 |
| 审计账本 | `/integrations/audit` | 查询 sverp-web 来源的账本事件（actor/outcome） |

实现要点（最小最佳实践）：
- BFF 代理聚合（浏览器不接触服务凭据；OAuth client_credentials 自动换 token）
- 页面路由/菜单由 Snaplink 权限控制（system:settings:read）
- aero-vault 修复了 access 禁用时的 PUT nil-panic（commit c0ce6db）

## 8. 集成页增强（2026-08-06 第二轮）

- **Vault presign 直传**：页面上传走 presign PUT（BFF 只签发签名 URL，文件
  流不经过 BFF 内存）——路线图 M3 最佳实践；下载为 presign GET 直链；列表可删除
- **质检单自动通知**（M5 最小版）：创建质检单 → aero-im "SVERP Integration"
  频道自动消息（操作人/款号/数量），best_effort 失败不阻塞业务（审计仍入账）；
  实测 NOTIFY-TEST-001/002 均在 8s 内送达
- **M3 遗留**：snaplink-console 审计页仍为本地管理操作记录
  （audit_log_service.dart + LocalStorage），未接 governance 账本——统一审计
  时间线需 Flutter 侧改动（console 仓库）
