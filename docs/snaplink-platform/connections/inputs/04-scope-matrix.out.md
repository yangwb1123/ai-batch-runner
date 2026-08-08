这是 evidence 阶段的第 4 个输入任务：`04-scope-matrix.md`（scope 矩阵摘要）。已核对与 inputs 目录文件一致（摘要指向的完整草案在 `docs/snaplink-platform/scope-matrix-draft.md`，15KB，与摘要所述 4 flows + 横切规则相符）。作为证据归一，提取 kickoff/per-project 阶段可直接消费的 scope 清单、验证锚点与开放决策：

---

# 证据归一输出：Scope 矩阵证据（04-scope-matrix）

> 任务：`04-scope-matrix.md`（证据归一 · scope 提案矩阵摘要）。
> 等级标注：**Verified** = 代码/配置中已实装的锚点；**\[PROPOSED\]** = 提案名，pending snaplink scope-registry 落地与人工批准（`MULTI_REPO_OPERATIONS.md` 契约规则）。

## A. Verified 锚点（与 01-connections-evidence 交叉核对的真实凭据）

| 锚点 | 事实 |
|---|---|
| aero-im → vault BlobStore | 经 client_credentials 调用（连接 6 的 vault 验签面之外，vault 侧尚需 `vault:blob:*` scope 门禁） |
| aero-id gRPC 校验 | 连接 7 中 aero-id 的 snaplink:token:validate 备选已进入 scope 讨论（未定案） |
| connector 脚手架 | aero-id `connector/auditgovernance`、aero-vault `internal/auditgovernance/token.go`（05-integration-design §0 引用） |
| console BFF 规则 | 连接 8 的 nginx 代理是 Verified 现状；两 token 模型未落地 |
| 注册表 seed 名 | `vault:blob:read/write`、`audit:event:ingest`、`account:uid:resolve` |

## B. 提案 scope 清单（4 flows，归一为表）

| Flow | 授予方→消费方 | 提案 scope | 要点 | 关联连接 |
|---|---|---|---|---|
| 1 | vault → aero-im | `vault:blob:write`（仅 PUT）+ `vault:blob:read`（仅 GET，可弃） | 附件上传下载；**deny**：删除/admin/分享管理/导出/`vault:rag:*`（禁止静默 RAG 索引附件）/metadata 更新/用户级 scope/审计读回 | 6（vault 信任面）+ 未来 im→vault |
| 2 | snaplink → aero-id | `snaplink:token:validate`（introspection，claim 最小化：subject/tenant/expiry/scopes，**不含 profile PII**）**或** mTLS allowlist（二选一） | **deny**：全部 `snaplink:user:*`/`policy:*`/`client:*` 管理、批量 token 吊销、自授 `account:uid:resolve`（循环信任） | 7（aero-id gRPC 未定案） |
| 3 | governance → vault + im | `audit:event:ingest`（append-only、event_id 幂等、tenant 一致性检查） | **im 授予以 Outbox relay 实现为条件**（verified gap）；**deny**：read/export/legalhold/retention/integrity/compaction；`audit:event:update`/`delete` **必须不存在**（不可变账本） | 2（sverpweb 已用 audit:event:write——命名需对照）+ 7 |
| 4 | 各服务 → console BFF | 枚举 admin scopes：`snaplink:admin:user:manage`、`account:admin:user`、`vault:file:admin`、`im:admin:room`、`audit:event:read/export`、`audit:legalhold:apply` | 两 token 模型（用户 OIDC 会话 + BFF 服务账号）；绑定规则：每操作授权检查 + decision_id、view/write 分离、tenant 限定；**deny**：blob/用户级 scope、`audit:event:ingest`、通配符、无双人控制的 secret 轮换 | 5 + 8 |

## C. 横切 deny 规则（连接矩阵的合规过滤器，逐条适用）

1. 服务账号 token **不得携带用户 scope**（与 COMBINATION_MODEL §5 一致）。
2. 生产者**不得读回账本**（vault/im 只有 ingest，无 read——sverpweb 现有 sverp-audit 仅 `audit:event:write` 即合规）。
3. **不存在** delete/update/compaction 类 scope（不可变账本纪律）。
4. **无通配符** scope（admin 枚举式，禁 `*`）。
5. **一客户端一域**（one-domain-per-client）。
6. tenant 一致性检查贯穿。

→ 阶段 3 每个项目的"凭据现状核查"都要对照 C 表：现有服务账号（sso-admin/sverp-audit/sverp-vault）已实装名是否满足/违反这些规则。

## D. 6 个开放决策（阶段 3 分项目任务须逐条给处置或标注延续）

| # | 决策 | 归属项目 | 影响连接 |
|---|---|---|---|
| 1 | scope 注册表落地（P0-3 排序） | snaplink（registry landing） | 全部（凭据命名统一） |
| 2 | 附件归因的委托模型：服务账号 vs 按用户委托（RFC 8693） | aero-im + vault | 1/6 |
| 3 | gRPC 认证选择：`snaplink:token:validate` vs mTLS allowlist | aero-id + snaplink | 7 |
| 4 | aero-im Outbox relay 实施时序 | aero-im | 2/3 |
| 5 | admin scope 粒度拆分（枚举细度） | console + 各服务 | 8 |
| 6 | 双人控制流程（secret 轮换等） | console + snaplink | 8 |

## E. 与 evidence 01/03 的交叉结论

- **命名对照**：01 中 sverpweb 实装凭据 `audit:event:write`、`audit:policy:write`（sso-admin）是 **Verified 现状名**；B 表 `audit:event:ingest` 是 **[PROPOSED]**。governance 侧需明确 `write`（已实装）与 `ingest`（提案）是否同一语义——阶段 3 governance 分析必须核查 governance 实际校验的 scope 名，避免注册表落地时双名并存。
- **合规初判**：01 的 8 条连接现状均未见违反 C 表横切规则的实装凭据（sverp-vault read/write/admin 为文件域、sverp-audit 无读回、sso-admin 是控制面）；唯一未决面是连接 5 浏览器直连（缺 BFF 服务账号层，Flow 4 覆盖）。
- 与 03 组合纪律一致：Flow 4 的 per-operation decision_id 绑定正是 COMBINATION_MODEL §5 的落地方案；Flow 3 的 ingest 条件（relay 先于授权）符合"跨仓修改必须确实需要改变契约"的克制红线。

**归一结论**：scope 矩阵为连接分析提供**凭据侧目标态**（4 flows + 6 横切规则 + 6 开放决策）；kickoff 连接矩阵应增列"凭据目标态"列，阶段 3 各项目须核查现状 scope 名与提案名的对应/冲突，并对 6 开放决策中归属本项目的给出定案建议（带理由、影响面、验收标准）。
