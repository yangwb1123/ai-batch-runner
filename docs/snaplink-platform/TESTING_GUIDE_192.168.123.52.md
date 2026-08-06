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
