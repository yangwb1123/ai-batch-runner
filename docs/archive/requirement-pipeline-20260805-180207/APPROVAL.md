已产出 `docs/snaplink-platform/decisions/APPROVAL.md`：

- **change_id**：`COMPOSE-2026-019`（017 已被示例 manifest、018 已被上一轮流水线占用，无碰撞）
- **受影响仓库**：4 个实施变更（aero-vault Primary、snaplink 权限种子、aero-im 模板种子、snaplink-console 文件管理页）+ 1 个零代码须 staging 验证（snaplink-audit-governance）+ 显式不受影响（aero-id，被调用 ≠ 受影响）
- **部署顺序**：snaplink 授权 → vault 迁移部署 → im 模板（首次通知前）→ console → staging 验证账本 ingest 后放行；附回滚原则（删除不可逆、账本永不回滚）
- **PROPOSAL 声明**：明确人工批准前不得开始任何仓库实现；变更 manifest 必须经 `check-change-manifest.py --map platform-map.yaml` 机械校验（列明校验字段与必含失败策略）；遗留风险如实移交；批准记录表留空待人工签署
