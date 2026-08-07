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

## 9. ERP 业务功能（2026-08-07 新增）

| 功能 | 入口 | 说明 |
|---|---|---|
| **请购流程** | 左侧菜单 ERP → 请购流程（或 /purchase-requests） | 创建 → 提交 → 审批/驳回；状态机 + 全生命周期审计入账 |
| **通知铃铛** | 顶部铃铛图标 | 30s 轮询"待我审批"；红点计数；点击列表项跳转请购页 |
| **我的资源** | 右上角用户菜单 → 我的资源（/my/resources） | 个人文件 presign 直传/下载/删除；命名空间 user/{账号}/ 隔离 |
| **操作记录** | 用户菜单 → 操作记录（/integrations/audit） | governance 账本查询（全部来源事件） |

**请购审批集成演示**（两账号协作）：
1. tester 登录 → 请购流程 → 新建（款号 PR-xxx）→ 提交审批
2. admin 登录 → 顶部铃铛出现红点 → 点开看到"请购审批：PR-xxx（tester 提交）" → 点进请购页 → 通过
3. 操作记录页可见 created/submitted/approved 三条审计事件（actor 正确）
4. aero-im "SVERP Integration" 频道同步收到审批消息（best_effort）

**我的资源隔离**：admin 上传的文件 tester 看不到（prefix 隔离）；tester 直接调
删除 admin 的 key 返回 403（服务端前缀校验）。

## 10. 路由 404 排查记录（2026-08-07）

**现象**：`/my/resources` 等新页面浏览器访问 404。

**根因（三层）**：
1. sverpweb 路由由 **system_route_definitions 表**（数据库）驱动——仅加前端
   routes.ts 不够，必须插表（`/api/system/runtime-routes` 是前端数据源）
2. 表的 `parent_id` 必须用 `built-in-<base64(route_key)>` 格式（如
   `built-in-c3lzdGVt` = system），不能用 route_key 原文
3. Snaplink `permissions_menus` 是**授权边界**（definition 必须匹配会话菜单
   id/path + 权限）——菜单被后续写入覆盖后须重加，且**已登录会话要重新登录**
   才能拉到新菜单快照

**修复**：6 条路由插表（integrations×4/purchase-requests/my/resources）+
parent_id 修正 + 菜单重加 → runtime-routes 22→30 全齐。

**以后加页面三步**：① routes.ts（菜单定义+组件映射）② system_route_definitions
插表（parent_id 用 built-in 格式）③ Snaplink permissions_menus 加菜单 →
重新登录。

## 10. 审计与软删除体系（2026-08-07）

**审计页**（操作记录 / 集成页-审计账本）展示：
- 时间列：操作人 @ 本地时间 做了什么（人类可读描述）
- 变更明细：新增（内容）/ 删除（删除前快照）/ 修改（字段: 旧值 → 新值）
- 事务链：operation_id 可点击 → 过滤同一事务全部操作（创建→更新→删除
  一条链），再点清除

**软删除 + 审计字段**（qa_prod_check / contents / media / purchase_requests）：
- 每表：created_at/created_by、updated_at/updated_by、deleted_at/deleted_by
- 删除一律软删除（UPDATE 记录删除人+时间），列表/详情过滤 deleted_at IS NULL
- 实测：删除内容 → DB 行保留 deleted_by=admin + 列表不可见

## 11. iris-ui 组件修复与同步（2026-08-07）

**已修复的交互缺口**（下拉/选择类选项无 hover 反馈）：
- List（vue/react/solid/svelte 四框架）、react Select、vue/react Cascader、
  vue/react TreeSelect——选项行 hover 高亮（--iris-surface-hover）
- combobox 原本已有 hover（onMouseEnter→active）

**同步通道**：iris-ui 依赖包（core/tokens 等）未发布 npm（Alpha）——
pnpm overrides 不可行；`bash scripts/sync-iris-ui.sh`（构建 vue 包→覆盖
node_modules→rm dist 强制重建→bundle 校验 MouseEnter）为当前通道；
iris-ui 发布 npm 后改为升级依赖即可。
