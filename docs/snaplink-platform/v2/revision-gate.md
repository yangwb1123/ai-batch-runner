# Gate — v2 契约包（registry/scope-matrix/ingestion-design + 实施清单）对抗审查核对

> 核对基准：10 个阻断项 ×（契约层已修订 / 实现前置已入清单且未声称完成）+ 六份 v2 对抗审查新证据。

## 逐项核对

### S1（dev-token 默认关闭）
- 契约层：**已修订** ✓ — ingestion v2 §1.7 生产信任姿态（默认 false、dev-only 拒绝启动、manifest CI 扫描）。
- 实现前置：**已入清单** ✓ — B1-1（sink `cmd/audit-api/main.go:45` + `ValidateConfiguration` + verify compose），未声称完成。
- 审查确认：DS-01/SRE-01/V2-2/security 四路重验代码默认仍 true —— 符合"待实现"预期；QA 补齐三个缺失测试（HTTP 401 sweep / config 拒绝 / manifest 扫描）已映射 T-1.1。

### S2（IdP token 带 tenant_id/roles）
- 契约层：**已修订（要求正确）** ✓ — registry v2 §1.2 必填字段 + scope-matrix v2 Flow 2 claim 契约。
- 实现前置：**已入清单但不可供应** ✗ — B4-1 钉错代码库：部署 IdP = `/home/u1/workspace/demo/snaplink`（`yangwb1123/snaplink`，**已 stamp kid+jti**、introspection 已认证、audit postgres+hash_chain），清单引用的 `token/jwt/generator.go:52-55`/`sso.go:894-960` 属未部署 legacy 树（`github.com/opensso/sso`），**3/5 行文件在部署树不存在**；"补 kid"前提错误（已存在）。protocol V2-5、database F-13、SRE-02、SEC-V2-3 四路独立确认。claim 契约 provenance 引用的 protocol F-2 亦基于错误树。
- 结论：**契约要求成立，但实施清单按现行文件不可供应**——与原始 gate 对 F2 的判罚同类（不可供应的契约/清单不成其为契约）。

### F2（scope 名对齐）
- 契约层：**已修订** ✓ — scope-matrix v2 六处全改 `audit:event:write`（机械校验：旧名仅剩删除说明）+ registry v2 命名空间注释 + ingestion v2 全改。
- 实现前置：vault B3-5 "无代码改动"经 security V2-13 复核**准确** ✓；scope registry B4-2 已入清单 ✓。
- 审查确认：vault `RequiredScope` 严格检查在部署树成立。

### F3/H1（vault 死信 + Ready 解耦）
- 契约层：**已修订** ✓ — ingestion v2 §1.4 normative（终态 ≤1 次、重试 cap、dead 行排除）+ readiness 解耦条款（degraded + maxLag×0.5，绝不 503）。
- 实现前置：**已入清单** ✓ — B3-1（migration 0039 + `relay.go` Permanent 分类）、B3-2（`runtime.go` Ready 解耦）、B3-4（metrics）；未声称完成。
- 审查确认：代码仍 retry-forever、`Ready()` 仍 503（DS-02/SRE-04/security S3）—— 符合预期；QA 指定 T-3/D1 测试。

### F4（确定性 event_id）
- 契约层：**已修订** ✓ — registry v2 §1.9（公式 + 四层去重键 + 再生规则 + 防 squatting）+ ingestion v2 §1.3。
- 实现前置：**已入清单** ✓ — B3-3（vault `facts.go`）、B2-4（aero-id 三调用点）；未声称完成。
- 审查确认：仍 `uuid.NewString()`/`idgen.NewID()`（DS-04、QA T-4）—— 符合预期。

### F5（aero-id in-tx 审计）
- 契约层：**已修订但数据不准确** △ — ingestion v2 路径清单表（8 个 post-commit 点标 Verified）；security 实测为 **49 个 `_ =` 站点**——契约"Verified"计数错误。
- 实现前置：**已入清单** ✓ — B2-1（含全仓 grep 门禁，要求本身仍成立）；未声称完成。
- 结论：验收门禁不受影响（grep 门禁覆盖任意数量），但契约文档事实声明必须修正计数。

### F-01（sink 快照冲突重试）
- 契约层：**已修订** ✓ — ingestion v2 sink 节（冲突重试、域错误映射、fsync、readyz store probe、relational-or-budget）。
- 实现前置：**已入清单** ✓ — B1-2/B1-3；未声称完成。
- 审查确认：QA 实测 store 层 PG 测试已绿（T-6 部分）；HTTP 层并发测试、readyz probe、sink CI workflow 仍缺 —— 符合预期并已列入验收。

### F-06（读路径审计 + console 重标注）
- 契约层：**已修订** ✓ — registry v2 console 重标注（local-only today）+ `audit.event.read`/`audit.event.export` 入表 + ingestion v2 console 节。
- 实现前置：**已入清单，一处路径错误** △ — B1-5 ✓；B6-1 引用的 `lib/services/portal_api.dart` **不存在**（QA 实测实际路径 `lib/api/portal_api.dart`）。

### F13（sink self-audit writer + 拓扑）
- 契约层：**已修订（部分）** △ — self-audit writer normative P0 ✓、支持拓扑声明 ✗：契约声明"HTTP ingest API + self-audit writer 为唯一受支持入站路径"，但 DS-09 验证 **gRPC ingest 是活动入站且未声明**——契约描述与已验证实现矛盾（同 F2 类），v2 未修订也未列入关闭任务。
- 实现前置：B1-4/B1-6 已入清单 ✓；未声称完成。

### F-3（discovery 真值化）
- 契约层：**已修订** ✓ — registry v2 §1.10 事实表 + scope-matrix v2 Flow 2 endpoint contract。
- 实现前置：**已入清单但目标树存疑** ✗ — B4-3 钉在 legacy 路径（`cmd/server/routes.go`）；security 判定 V2-3 discovery 缺陷属非部署树，QA 在"部署仓 @ ac5d367"实测两个 discovery 测试 PASS 且 codify 错误契约（`/authenticate`、`8080:0`）——两路结论冲突的根源正是 B4 双树未分辨；QA 另要求**重写/删除两个测试**（`TestOIDCDiscovery` + `TestOIDCDiscoveryEndpoint`，均为缺陷回归锁），清单只写了前者。

## 剩余阻断（v2 审查新证据，gate 判罚依据）

1. **B4 双树混用（S2/F-3 实施前置不可供应）**：3/5 行指向未部署 legacy 树；"补 kid"已存在；claim 契约 provenance 引用错树审查。四路独立确认（V2-5/F-13/SRE-02/SEC-V2-3）——按清单实施"shipping nothing or risks deploying the weak tree"（SEC-V2-3）。
2. **DS-08/SRE-07：sink 静默覆写 envelope `tenant_id`（以 token tenant 覆盖而非 422）** —— 与 ingestion v2 契约"mismatch → 422 reject"直接矛盾；v2 未修订、未列实施任务（与原始 F2 同类：契约保证 vs 已验证实现矛盾）。
3. **F5 契约计数错误**：8 vs 49 个 `_ =` 站点（security 实测）。
4. **DS-09：活动 gRPC 入站未被拓扑声明覆盖**（F13 契约部分矛盾）。
5. **B6-1 console 路径错误**（`lib/services/` vs 实际 `lib/api/`）+ **B4-3 需重写两个 discovery 回归测试**（QA）。

## 结论

契约层五项（F2/F4/F-06/F13/S2 的规范文本）确已修订且可验证；十项实现前置除 B4 外均已正确入清单、未声称完成；sink/producer 侧经审查验证正确（protocol 底行确认）。但 B4 钉错代码库使 S2/F-3 的实施前置**按现行清单不可供应**，且审查暴露两处契约-实现新矛盾（tenant_id 覆写、gRPC 拓扑）与两处清单数据错误（49 个 `_ =`、console 路径）——按 gate 的 fail-closed 判罚传统（原始 gate 正是因"已验证现实与前提矛盾"而 FAIL），v2 包需修正后重审：重钉 B4 至部署仓、修正 claim 契约 provenance 与 F5 计数、增列 tenant_id 覆写对齐任务与 gRPC 拓扑收敛、修正 B6-1 路径与 B4-3 双测试。

VERDICT: FAIL - B4 双树混用致 S2/F-3 实施前置不可供应（3/5 行指向未部署 legacy 树、"补 kid"已存在、provenance 引用错树审查）；DS-08/SRE-07 sink 静默覆写 tenant_id 与契约 422 矛盾未修订未列任务；F5 契约计数 8 vs 实测 49 个 _= 站点；DS-09 活动 gRPC 入站未被 F13 拓扑声明覆盖；B6-1 console 路径错误与 B4-3 需重写两个 discovery 回归测试——修正上述五项后 v2 可进入人工批准
