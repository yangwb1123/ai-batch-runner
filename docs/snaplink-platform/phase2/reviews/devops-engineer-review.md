# DevOps 工程评审 — 第二阶段（构建/交付/部署自动化）

> 角色：devops_engineer。评审对象：`docs/snaplink-platform/phase2/kickoff.md`
> （正文与已验证 `/tmp/kickoff-final.md` 一致，预验证全绿）+ 部署证据
> `DEPLOYMENT_192.168.123.52.md`、`TESTING_GUIDE_192.168.123.52.md`、
> `ERP_SCENARIO_ANALYSIS.md`、证据 05、本仓库实际资产扫描。
> **本仓库无 `ops/deploy/` 目录**（已 find 确认）；部署资产证据全部来自
> 服务器实录文档与证据 05，**示例 compose/脚本不视为可部署证据**。
> 本评审为咨询性分析，不构成发布批准。

## 1. 受支持的部署路径与产物清单（Verified / Missing）

### 1.1 实际部署路径（Verified，单主机）

唯一受证据支持的路径：**192.168.123.52（svmisdev, Debian 12, x86_64, 15Gi）单主机
systemd 直部署**，capistrano 风格 `current/shared` 目录、每服务独立二进制。
8 个单元 + nats 全 active（证据 05 / DEPLOYMENT §2-3）：

| 服务 | 端口 | 二进制/形态 | 依赖 | 证据 |
|---|---|---|---|---|
| snaplink-sverp | :13015/:13016 | sso-server minimal（sqlite+JWT） | — | Verified |
| snaplink-console | :13017 | nginx（Flutter Web 静态产物） | — | Verified |
| sverpweb-stable | :13014 | bun（Hono BFF + Vue3/iris-ui 0.2.x） | governance/vault/im | Verified |
| audit-governance | :13018 | audit-api 22M，文件账本 state.json 单节点 | snaplink Ed25519 PEM | Verified |
| aero-vault | :13019 | aero-vault-server 36M（sqlite+local） | snaplink JWKS | Verified |
| aero-id | :13020/:13021 | aero-id-server 45M（PG+local） | PG aero_id | Verified |
| aero-im | :13022 | aero-server 52M Rust release | PG aero_im+Redis+NATS | Verified |
| nats | :13023 | nats-server v2.14.4 JetStream | — | Verified |

- systemd 单元统一加固（NoNewPrivileges/PrivateTmp/ProtectSystem=strict/
  ProtectHome=read-only/RestrictSUIDSGID/LockPersonality/ReadWritePaths 仅 data），
  GOMEMLIMIT 256–384MiB 有界——**做对项**（Verified，DEPLOYMENT §2）。
- 部署模型：每服务独立二进制 + 原子 `current` 切换 + `shared` 数据/env 分离
  ——回滚无损模型真实成立（第一轮 devops 评审已实证，延续成立）。
- 探针：vault `/healthz`、aero-id `/healthz`+`readyz`、audit 401 门禁（Verified）。

### 1.2 版本化资产清单（本仓库实际扫描，2026-08-06）

| 资产 | 位置 | 状态 |
|---|---|---|
| `ops/deploy/` | **不存在** | **Missing**——评审指令指定路径零文件 |
| systemd 单元 / nginx 配置 / EnvironmentFile 模板 | 仅服务器 | **Missing**（未版本化） |
| `test-platform.sh` / `sync-sso-key.sh` | 仅服务器 `/home/ywb/` | **Missing**（kickoff 3.3#8 已列） |
| 平台脚本 | `examples/snaplink-platform/{platform-247,platform-status,sync-tool,wait-for-run}.sh` | 示例层，未与服务器逐字节核对 |
| compose/BOM | `examples/snaplink-platform/suite-template/compose/*.yaml`、`compatibility.yaml` | **示例**——实际部署是 systemd，非 compose；不得由此推断可部署性 |
| 部署实录 | `docs/snaplink-platform/DEPLOYMENT_192.168.123.52.md` | 叙述性记录，非可执行资产 |
| CI | 本仓 `.github/workflows/ci.yml` | 只门禁 ai-batch-runner（Python 工具仓），**与平台 7 仓无关** |

### 1.3 平台 7 仓门禁现状（Verified 手动 / Missing 自动化）

sverpweb-stable：`bunx tsc --noEmit` 0 错、`pnpm check` 9/9（154 api + 115 web）、
forwarding 8 用例、governance `erp_contract_test.go` 5 测试——**全部人工在本地跑**
（ERP_SCENARIO_ANALYSIS 0.1，Verified）；无任何平台仓的 .github/workflows 证据；
kickoff 1.2.4 把"pnpm check 入 git 钩子或 CI"列为 **Proposed**（未做）。

## 2. 流水线表（阶段 × 证据 × 缺口 × 建议门禁 × owner）

| # | 阶段 | 当前证据 | 缺口 | 建议门禁 | owner |
|---|---|---|---|---|---|
| P1 | 源码门禁 | tsc 0 / pnpm check 9/9 / go test / forwarding 8 全绿（Verified，**本地手动**） | 零 CI 自动化；无提交前强制 | 每仓 CI：PR + push 跑全套门禁（tsc/build/test/lint）；sverpweb 先入 git 钩子（1.2.4） | 各仓 owner |
| P2 | 构建/产物 | 最小化单二进制（22–52M，Verified）；sso-client 构建产物入库（commit d19838d8） | 无 artifact registry / 无 sha256 / 无保留策略；**曾发生服务器 dist 为 8/4 旧构建事故**（DEPLOYMENT §6） | 发布产物 = 构建日志 + sha256 清单 + 版本戳（git describe），部署前校验 | 平台 owner(devops) |
| P3 | 部署 | systemd 单元服务器就位；第一轮评审曾核对"单元与仓库 deploy/ 逐字节一致" | **本仓无 ops/deploy/**；单元/nginx/env 模板未版本化，漂移不可审计 | 全部单元入版本库；`systemd-analyze verify` + 部署演练（4.4） | devops |
| P4 | 配置/密钥 | .env 0600、PEM 0600、RSA keypair、EnvironmentFile 隔离（Verified） | keys.txt/accounts.txt 明文可读；PG 口令 aero_local_dev_only（dev 级）；三个 secret "应轮换"未执行（DEPLOYMENT §6） | 4.2 全量轮换 + 密钥清单表 + secret 扫描钩子；grep 扫描零命中 | 平台 owner |
| P5 | 探针/健康 | status 25 项 + audit 401 门禁 + /healthz、/readyz（Verified） | console HTTP 探针缺失（曾 inactive 一次、根因未记录、无 Restart=on-failure）；无 drainer 稳态检查 | console 200 探测入 status；`Restart=on-failure`；24h pending=0/permanent=0 观察（L2 指标） | devops |
| P6 | 迁移 | aero-id/aero-im auto_migrate 全跑（含 vector）（Verified） | sverpweb audit_outbox **隐式迁移无版本戳**；governance 单节点无复制；vault 迁移未启动（依赖备份） | 迁移框架统一（版本戳 + 前后校验）；vault 回填校验脚本（1.2.5） | sverpweb/vault owner |
| P7 | 备份/恢复 | **零脚本（Missing）**；governance state.json = 审计真源无备份；第一轮评审已记备份过期至 07-29 | 无 RTO/RPO、无恢复演练、无异地目标 | 4.3：PG pg_dump + sqlite .backup + rsync；`test-platform.sh backup` 子命令；月度演练 | devops |
| P8 | 发布/回滚 | current/shared 原子切换模型成立（Verified） | 全手动 tar/scp/切链接，无审批留痕、无 preflight、无脚本化回滚 | 4.4 runbook 文档化 + 演练；回滚=切 previous + e2e 复验 | devops |
| P9 | 监控/告警 | 无 /metrics、无告警（Missing） | 巡检无健康阶段；看板无 governance 账本行 | 4.1：platform-247 每轮加 status 阶段 + 账本行 + aero-im 告警 sink | devops |
| P10 | 密钥轮换语义 | **重启即轮换（rotation.enabled=false 仍轮换）**（Verified 缺陷） | 重启=全链路认证失效窗口 + 审计 401；L2 前置未绿 | 1.2.1：显式轮换 + 跨重启指纹单测 + 重启演练 | snaplink owner |
| P11 | 环境一致性 | 无 staging（Missing） | 直改生产；灰度=staging 同版本验证是 Proposed | staging 同版本验证后再替换（4.4#4） | devops |

## 3. Findings（按严重度）

### Critical

**C1 — 部署资产零版本化（`ops/deploy/` 不存在，单元/脚本只在服务器）**
- 位置：本仓库 `ops/deploy/`（Missing）；服务器 `/home/ywb/apps/*/`、`/home/ywb/{test-platform.sh,sync-sso-key.sh}`。
- 影响：部署不可复现、不可审计、不可回滚到已知状态；服务器重建 = 全手工；测试脚本与平台状态漂移（3.3#8 自认缺口）。生产声明的基础设施侧证据断裂。
- 修复：把 systemd 单元、nginx 配置、EnvironmentFile 模板、test-platform.sh/sync-sso-key.sh 全部入 suite 版本库（`ops/deploy/`），Change Manifest 门禁；服务器 `systemd-analyze verify` 校验一致性。
- 验证：`git ls-tree -r --name-only HEAD ops/deploy/` 非空且与服务器逐字节 diff 为零；`test-platform.sh` 从版本库检出运行全绿。

**C2 — snaplink 重启即轮换签名密钥（rotation.enabled=false 无效）**
- 位置：snaplink 配置 `rotation.enabled`（证据 05 §4 / kickoff 1.2.1 Verified）。
- 影响：任何重启 = 存量 JWT/refresh 全失效 + governance/vault 公钥过期 + 审计投递 401 窗口（TESTING_GUIDE FAQ 实证）；**L2 宣布前置未绿**，也直接破坏 4.4 升级流程的可行性。
- 修复：kickoff 1.2.1——禁用轮换时密钥持久化，仅显式 `rotate` 命令轮换并输出新旧 kid 告警；短期保留 timer 兜底。
- 验证：`systemctl restart snaplink-sverp` 后三套件全绿且无需手工 sync-sso-key.sh；新增单测：rotation.enabled=false 跨重启密钥指纹不变。

**C3 — 备份零脚本，审计真源单节点无备份**
- 位置：governance `state.json`（单节点文件账本）；sverpweb sqlite+本地媒体；PG aero_id/aero_im；snaplink sqlite。
- 影响：审计真源（3.1 裁定）持久性完全依赖单文件；sverpweb 媒体在**无备份的服务器本地目录**（1.2.5 迁移动机）；第一轮评审已记备份过期（07-29 后无备份）。一旦磁盘故障：审计历史 + 业务数据双失。
- 修复：kickoff 4.3——优先级 state.json > snaplink sqlite > sverpweb sqlite/媒体（迁移前双备份）> vault > PG；`test-platform.sh backup` 子命令 + 月度恢复演练；备份目标与服务器分离。
- 验证：演练从备份恢复 governance 账本后查询 + E2E 全绿；账本行数/字节与备份前一致。

### High

**H1 — secrets 明文与 dev 级口令未轮换**
- 位置：`/home/ywb/.platform-test/keys.txt`、`accounts.txt`（明文可读）；PG 口令 `aero_local_dev_only`；sso-admin/sverp-audit secret（DEPLOYMENT §6 注明"应轮换并移入密钥管理"）。
- 影响：凭据泄露面 = 全部服务账号 + admin 密码；dev 级 DB 口令可被任意本机进程读取。
- 修复：4.2 全量轮换（admin/tester + 三服务账号 + PG 口令）；密钥清单表（位置/用途/轮换周期/owner）；secret 迁入 systemd EnvironmentFile（0600 逐服务隔离）；提交钩子扫描防入库。
- 验证：`grep -rn '<旧 secret>' /home/ywb` 零命中；`ssh svmisdev 'stat -c %a keys.txt'` = 600；CI secret 扫描 hook 存在。

**H2 — 平台 7 仓无 CI 自动化**
- 位置：各平台仓（无 .github/workflows 证据）；本仓 ci.yml 只覆盖工具仓。
- 影响：门禁全绿依赖人工自觉，历史已发生"tsc 31 错门禁红"与"旧 dist 部署"事故；7×24 轮巡（4.1）与 CI 无交集，回归无自动护栏。
- 修复：每仓 CI（PR+push 触发全套门禁）；sverpweb 先落地 git 钩子 + CI（1.2.4）；governance go vet/test 入 CI。
- 验证：提交一个故意坏改动到任一仓 PR → CI 红；门禁脚本与本地 `pnpm check` 输出一致。

**H3 — 发布全手动、无审批留痕、无产物校验**
- 位置：tar/scp/切链接流程（第一轮评审 C2 延续）；"服务器 dist 曾为 8/4 旧构建"事故（DEPLOYMENT §6）。
- 影响：无法回答"线上跑的是哪个 commit"；部署事故只能靠人工发现；发布不可审计。
- 修复：P2 门禁——发布包带 git describe + sha256，部署脚本校验后再切 current；4.4 runbook；决策日志记录每次发布（approval 留痕）。
- 验证：`deploy.sh --check` 在版本不匹配时拒绝启动；决策日志含最近一次发布记录。

**H4 — 无监控/告警，console 曾 inactive 且无自愈**
- 位置：console 单元（曾 inactive 一次，根因未记录，无 Restart=on-failure）；全平台无 /metrics/告警。
- 影响：服务静默失败不可见；"审计停了"只能人工发现；7×24 巡检（4.1）无健康阶段。
- 修复：console 加 `Restart=on-failure` + HTTP 200 探测（2.5/3.3）；platform-247 每轮加 `test-platform.sh status` 阶段；看板加 governance 账本行；aero-im 告警 sink（4.1）。
- 验证：`systemctl kill -s KILL snaplink-console` 后 5s 内自动恢复；人为停 governance 后 2 轮内 summary 出现告警记录。

**H5 — DEMO_PASSWORD / AUDIT_APP_TOKEN 残留，无 fail-closed**
- 位置：sverpweb .env（DEMO_PASSWORD 死配置；AUDIT_APP_TOKEN 静态 token）。
- 影响：demo 后门配置仍在（N2 待办）；静态 token 提供静默降级路径，掩盖 OAuth 自动轮换缺陷；L2 前置"demo 禁用"未达成。
- 修复：2.2/2.3——删除两键；配置校验 fail-closed（未知/demo 键启动即拒）；transport 侧 OAuth 未配置时禁止回退静态 token。
- 验证：`grep -rn DEMO_ apps/`、`grep -rn AUDIT_APP_TOKEN apps/` 零命中（docs 除外）；移除后 e2e 审计链路全绿。

### Medium

**M1 — 迁移体系不统一**：sverpweb audit_outbox 隐式迁移无版本戳（ERP_SCENARIO_ANALYSIS 本轮额外发现）；governance 单节点无复制；vault 迁移（1.2.5）依赖备份就位。→ 统一迁移框架（版本戳 + 前后校验），vault 迁移前先完成 4.3。

**M2 — 无 staging/环境一致性**：直改生产；4.4#4 灰度=staging 同版本验证仍是 Proposed。→ 首次升级前至少做一次"staging 同版本验证 → 生产替换"演练。

**M3 — 健康探针不完整**：无 console 探测、无 drainer 稳态检查（pending=0/permanent=0 24h）、无 /metrics。→ 3.3 缺口清单 1/7 绑定对应里程碑验收。

**M4 — 测试资产未版本化**：test-platform.sh（25/8/8 断言资产）只活在服务器。→ 入 suite 仓库 ops 资产（3.3#8），Change Manifest 门禁。

### Low / Info

**L1 — HTTPS+JWKS 未做**（PEM timer 2min 同步 + 客户端重试实测可接受）：决策合理，但豁免理由与触发条件（出内网/多主机/合规）需**文档化**（2.1，当前为 Proposed）。已具备"重试+幂等安全网"兜底（做对项）。

**L2 — 做对项（延续）**：systemd 加固单元 + GOMEMLIMIT 有界；current/shared 回滚无损模型；不可变 release 思想；outbox 幂等安全网；逐事件 receipts 防毒丸饿死。

## 4. 发布与回滚流程（runbook 草案，Proposed）

> 依据 kickoff 4.4 + 本评审门禁补强。1.2.1 落地前，**任何重启都触发全链路认证
> 失效窗口**——本 runbook 只在密钥轮换语义修复后成立；修复前禁止非必要重启。

### 4.1 Preflight（发布前）
1. 三套件全绿：`bash /home/ywb/test-platform.sh {status|audit|e2e}`（8 服务 + 11 端口 + 审计 401 门禁）。
2. 备份（4.3 就位后）：PG pg_dump + sqlite .backup + state.json 快照；备份校验（行数/字节）。
3. 配置 diff：目标服务 .env/EnvironmentFile 与版本库模板 diff 为零（未知键 fail-closed 检查）。
4. 产物校验：发布包 sha256 + `git describe` 版本戳与目标 commit 一致（P2 门禁）。
5. 低峰窗口 + 维护窗口宣布；记录进决策日志。

### 4.2 发布顺序（依赖链序，sverpweb 最后）
备份 → 基础设施（PG/Redis/NATS）→ snaplink → governance/vault/aero-id/aero-im → console → **sverpweb 最后**（BFF 可对上游重试）。

### 4.3 每步后探针
`test-platform.sh status`（含新增 console 200 探测 + drainer 指标）；重点服务加
`curl /healthz`（vault）、`/readyz`（aero-id）、audit 401 门禁（governance）。

### 4.4 Post-deploy
1. `test-platform.sh e2e` 全绿（登录 → 质检单 → 账本 actor=admin）。
2. 审计链路：pending=0、permanent 计数不增（drainer 60s 窗口）。
3. 密钥指纹检查（1.2.1 后）：重启演练验证跨重启指纹不变；显式轮换时按 4.5。
4. 决策日志追加发布记录（含审批留痕）；旧 current 保留 N 版（保留策略待定）。

### 4.5 显式密钥轮换 runbook（1.2.1 落地后）
备份 → `rotate` 命令（输出新旧 kid）→ 立即 `sync-sso-key.sh` → audit/e2e 全绿 → 宣布；
轮换记录进决策日志。**governance AUDIT_SIGNING_SECRET/ENCRYPTION_KEY 不得随意轮换**
（哈希链/加密历史依赖，备份优先——4.2 密钥清单表标注）。

### 4.6 回滚
1. 触发条件：任一步探针失败且 2 轮重试不过。
2. 动作：切 `previous` symlink（current/shared 模型现成）→ 重启受影响服务 → 4.3/4.4 复验。
3. 1.2.1 前：回滚本身也是一次重启——会再触发密钥轮换窗口，按 2min timer + 客户端重试兜底，验收 = e2e 最大失败窗口 ≤2min。
4. 回滚决策 + 结果记录进决策日志。

## 5. 阻塞生产声明的缺失资产/待决策项

| # | 缺失资产 / 决策 | 阻塞对象 | 处置引用 |
|---|---|---|---|
| B1 | snaplink 密钥轮换语义修复（重启不得轮换） | L2 宣布、4.4 runbook 成立性 | 1.2.1（owner: snaplink） |
| B2 | 备份体系（零脚本现状）+ 月度恢复演练 | vault 迁移（1.2.5）、审计真源持久性、生产声明 | 4.3（owner: devops） |
| B3 | secrets 全量轮换 + 密钥清单表（明文 keys.txt / dev 级 PG 口令） | 生产安全声明 | 4.2（owner: 平台 owner） |
| B4 | L2 前置收尾：DEMO_PASSWORD 清除 + 配置 fail-closed + HTTPS 豁免决策文档化 | L2 宣布 checklist | 1.2.2 / 2.1 / 2.2 / 2.3（owner: sverpweb + 平台 owner） |
| B5 | 部署资产版本化（`ops/deploy/` 单元/nginx/env 模板 + test-platform.sh/sync-sso-key.sh 入库） | 可复现性、可审计性 | 3.3#8 + 本评审 C1（owner: devops） |
| B6 | 平台仓 CI 自动化（当前全手动） | 回归护栏、发布可信度 | 1.2.4（owner: 各仓 owner） |
| B7 | 监控/告警接线（无 /metrics、无告警 sink、console 无探针无 Restart=on-failure） | 7×24 巡检有效性 | 2.5 / 3.3 / 4.1（owner: devops） |
| B8 | console 审计页接 governance（1.2.3）+ test-platform.sh console 检查项 | M3a 用户可见承诺 | 1.2.3（owner: console） |

**结论**：部署**运行面**（8 服务 + 探针 + 审计 L2 E2E）已被实测证据支持；但**交付/运维面**
（CI、产物溯源、备份、密钥、版本化资产、监控）仍整体处于 Proposed/缺失状态。
第一轮 devops 评审的 C1/C2/H2 的修复是**部分**达成（git init + tsc 0 + governance 实例
落地），其余（备份、CI 自动化、审批留痕、密钥轮换）仍开放。**在 B1–B4 落地前，
不得宣布 L2；在 B2 落地前，不得启动 vault 迁移（1.2.5）。**
