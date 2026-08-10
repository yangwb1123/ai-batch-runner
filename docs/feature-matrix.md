# Feature Matrix

本文件只登记已有当前代码或门禁证据的能力。历史生成报告不直接提升为功能。

| 需求来源 | 能力 | 状态 | 主要证据 |
|---|---|---|---|
| expansion A-I | 治理、预算、复用、会话、限速、多 agent、语义门禁、批准点 | Complete | `docs/requirements/STATUS.md` |
| USAGE_FEEDBACK P1-P7 | 聚合、dry-run、容忍失败、退出诊断、provider 冷却、锁、失败摘要 | Complete | `docs/USAGE_FEEDBACK.md` |
| LESSONS D2 | 任意 JSON 产物 schema 门禁 | Complete | `pbatch/json_schema.py`、`tests/test_json_schema.py` |
| LESSONS D4 | 外部环境测试漂移隔离 | Complete | `tests/test_advance.py` 的存在性 skip 与结构断言 |
| LESSONS_SNAPLINK H1-H5 | UI 事故规则与批量编辑护栏 | Complete | `docs/LESSONS_SNAPLINK.md`、`backend-specs/agent-guardrails.md` |
| AGENTS.md 组件表 | CLI、pipeline、meta、gate-fix、memory、campaign、assess/rules/eval | Complete | `make ci`、`pi-batch check` |
| 可移植性契约 | 核心三件套复制与 wheel 安装均可运行结构化门禁 | Complete | `tests/test_json_schema.py`、`tests/test_packaging.py` |

完成状态要求实现、文档和可执行证据同时存在。候选想法统一进入
`docs/deferred-backlog.md`，具备验收标准后再提升。
