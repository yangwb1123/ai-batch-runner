裁决完成，产物已写入 `docs/snaplink-platform/connections/gate.md`。裁决前对 4 处争议事实独立复核了实际代码（scope.go:86 规则②、audit_ledger.go:79 错误吞掉、迁移 0037 唯一约束、cmd/server/auth.go:42 刷新接线），终裁如下：

---

# 裁决门：6 项目连接分析终裁

**VERDICT: PASS - 连接矩阵与 7 份分项目文档全部满足四裁标准：每连接有接口/凭据/状态证据（6 仓库代码级 Verified，sverpweb 已知证据缺口已记录决策日志）；29 项改动全部带理由/影响面/验收标准；六份对抗审查无架构级阻塞，5 处证据精度修正逐条裁定（4 采信 1 驳回，均经独立复核）；克制清单在全部 7 份文档保持。裁决附下游硬条件（QA-1/QA-2 测试先行、sverpweb 复核前置）与已裁定修正引用。**

**标准一（连接证据）— 满足**：kickoff 8 条连接矩阵逐条含发起方/接收方/接口/凭据（sso-admin、sverp-audit、sverp-vault、console admin token、im app token、aero-im-vault client_credentials）/状态（6 对接、1 部分、1 未落地）/合规初判；6 个本机仓库接口清单代码级 Verified，sverpweb 不可达标 [doc] 并记录为已知证据缺口（非方向性，下游实现前须真实复核）。

**标准二（改动三要素）— 满足**：7 项目 29 项改动（snaplink C-1..C-5、aero-id C-1..C-4、aero-vault C-1..C-5、aero-im C-1..C-4、audit-governance C-1..C-4、console B-1..B-4、sverpweb B-1..B-4）逐项含理由/影响面/验收标准；跨文档方向一致（本地 JWKS、审计读写分离、schema 审批链、share-link deferred、双 token 前置链）。

**标准三（对抗阻塞）— 满足**：六份审查均无架构级阻塞。变更级 QA-1（aero-im 连接 4 零测试）、QA-2（空 AllowedScopes=不限设，实测 scope.go:86 确认）转为对应改动验收的测试先行硬条件。5 处精度修正裁定：采信 B-1/QA-3（audit_ledger.go:79 `_ = publisher.Publish()` 实测确认，"幂等兜底"≠"丢失可发现"，补告警触发信号）、B-2/DB-2（vault 删除 commit→emit 崩溃窗口，补 kill-point 测试）、P-2（gRPC 死路径=熔断降级非无条件 fail-open）；B-3 采信 database_architect 再修正（部门表**有** `UNIQUE (tenant_id, parent_id, name)`，实测迁移 0037，"无唯一约束"不成立，缺口在调用方差集与根级 name）；**P-5 带证据驳回**——实测 `cmd/server/auth.go:42` 恒将 `AUTH_JWKS_KEY_TTL`（默认 3600）映射为 SDK 刷新间隔，snaplink C-3 的 3600s grace 基准对 stock vault 成立，仅保留 env=0 回落提示。FE-1/2/3、DB-1、P-3 均已有处置路径并入对应改动前置，无悬置。

**标准四（克制）— 保持**：vault 不接分享/单向同步/lock 不开 BFF；im DM deferred 带触发条件、block/mute/legal-hold 不暴露；governance 不统一账本、不加读、不绕过审批；console 不升级浏览器 token（禁令命中项以 BFF 正解处置）、不预代理、本地日志不上报；snaplink 不新增上报、不引 OPA；aero-id 不落地组织模型。

**下游条件**：① aero-im C-1 与 snaplink C-1 验收必带测试（fail-closed）；② 5 条已裁定修正作为实现输入引用，防止按"同事务/有兜底"错误表述编码；③ sverpweb 相关实现前先对真实仓库复核连接证据。
