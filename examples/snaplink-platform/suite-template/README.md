# Snaplink Suite — 组合仓库模板

这个仓库**不承载任何业务代码**。它只负责把 6 个独立产品
（snaplink / snaplink-console / snaplink-audit-governance / aero-id /
aero-vault / aero-im）按需组合成不同产品形态：本地一体化开发、
生产分布式部署、部分组合（如 snaplink + aero-vault）。

## 目录

```
snaplink-suite/
├── AGENTS.md              组合仓库执行契约（Agent 规则）
├── README.md
├── compatibility.yaml     版本兼容矩阵（可被门禁校验）
├── compose/               多种部署拓扑（minimal / identity / vault / full）
│   ├── minimal.yaml       snaplink 单独
│   ├── identity.yaml      snaplink + aero-id + console
│   ├── vault.yaml         OIDC Provider + aero-vault（标准协议组合示例）
│   └── full.yaml          完整 6 服务 + 集成依赖（Kafka/NATS/MinIO/Redis）
├── contract-tests/        契约测试（OpenAPI/事件 Schema 兼容）
├── e2e/                   端到端场景（见 e2e/README.md 清单）
├── configs/               各服务示例配置（integrations 段落模板）
├── pi-batch.yaml          ai-batch-runner 验证器（suite 门禁）
├── scripts/
│   └── suite-check.py     纯 stdlib 门禁：compatibility + compose 一致性
└── docs/
    └── topologies.md      拓扑说明与选择指南
```

## 初始化

```bash
# 用 ai-batch-runner 的模板初始化 suite 仓库
cp -r <ai-batch-runner>/examples/snaplink-platform/suite-template/. ~/workspace/demo/snaplink-suite/
cd ~/workspace/demo/snaplink-suite
python scripts/suite-check.py        # 门禁自检
```

## 与 ai-batch-runner 的关系

- 6 个产品仓库各自携带 ai-batch-runner，独立跑 campaign / advance / 7×24。
- 本仓库是**组合验证层**：跑组合 E2E 流水线、兼容矩阵回归、部署拓扑冒烟。
- 跨仓库契约草案（如新审计事件类型）在**产品仓库各自产出的提案**基础上，
  由本仓库汇总为兼容矩阵与事件目录的变更（需人工批准）。
