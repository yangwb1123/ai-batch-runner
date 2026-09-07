# Feature Matrix

本文件只登记已有当前代码或门禁证据的能力。历史生成报告不直接提升为功能。

| 需求来源 | 能力 | 状态 | 主要证据 |
|---|---|---|---|
| expansion A-I | 治理、预算、复用、会话、限速、多 agent、语义门禁、批准点 | Complete | `docs/requirements/STATUS.md` |
| USAGE_FEEDBACK P1-P7 | 聚合、dry-run、容忍失败、退出诊断、provider 冷却、锁、失败摘要 | Complete | `docs/USAGE_FEEDBACK.md` |
| LESSONS D2 | 任意 JSON 产物 schema 门禁 | Complete | `pbatch/json_schema.py`、`tests/test_json_schema.py` |
| LESSONS D4 | 外部环境测试漂移隔离 | Complete | `tests/test_advance.py` 的存在性 skip 与结构断言 |
| LESSONS_SNAPLINK H1-H5 | UI 事故规则与批量编辑护栏 | Complete | `docs/LESSONS_SNAPLINK.md`、`backend-specs/agent-guardrails.md` |
| AGENTS.md 组件表 | CLI、pipeline、meta、gate-fix、memory、campaign、assess/rules/eval、product brief | Complete | `make ci`、`pi-batch check`、`tests/test_product.py` |
| 可移植性契约 | 核心三件套复制与 wheel 安装均可运行结构化门禁 | Complete | `tests/test_json_schema.py`、`tests/test_packaging.py` |
| TUI 恢复闭环 | scope 内 session 索引、可信投影恢复、能力驱动 Codex resume、失败只读 | Complete | `pbatch/tui_sessions.py`、`tests/test_tui_sessions.py` |
| TUI Plan/Changes Inspector | 同源结构化 Plan、只读 workspace status/last-turn delta/有界 diff overlay | Complete | `pbatch/tui_inspector.py`、`tests/test_tui_inspector.py` |
| TUI Completion Inspector | typed turn facts + authoritative Git delta 汇总 outcome/commands/checks/risks/usage，正文不可伪造 | Complete | `pbatch/tui_completion.py`、`tests/test_tui_completion.py` |
| TUI Artifact/command output 闭环 | typed refs、workspace/data no-follow 预览、Codex 大输出私有 bounded ring | Complete | `pbatch/tui_artifacts.py`、`pbatch/tui_command_artifacts.py`、`tests/test_tui_command_artifacts.py` |
| Codex App Server + typed approval | 官方 stdio/initialized 握手、显式主 TUI runtime、typed modal/审计、三态 terminal fail-closed 投影 | Complete（sandbox 有效值常驻另行推进） | `pbatch/tui_adapter_appserver.py`、`pbatch/tui_approvals.py`、`tests/test_tui_adapter_appserver.py`、`tests/test_tui_approvals.py` |
| Codex provider 恢复对账 | `thread/read` 非订阅摘要、状态/ID 严格校验、active/error 历史只读、steer 响应确认 | Complete | `pbatch/tui_adapter_appserver.py`、`pbatch/tui_sessions.py`、`tests/test_tui_adapter_appserver.py`、`tests/test_tui_sessions.py` |
| TUI session 索引摘要 | projection-commit 后 byte-bound sidecar、typed `thread/list` page、exact advisory join、异步 picker worker | Complete | `pbatch/tui_session_summary.py`、`pbatch/tui_sessions.py`、`tests/test_tui_session_summary.py`、`tests/test_tui_sessions.py` |
| Pi canonical CLI 调用面 | `pi-batch pi` 原样 argv/stdio/cwd/env 透传；仅选择 Pi executable，非 Pi adapter 隔离 | Complete | `pbatch/pi_cli.py`、`tests/test_pi_cli.py`、`tests/test_entrypoints.py` |
| Pi RPC 产品 runtime | TUI 托管本地 LF-JSONL RPC、request id 配对、权威完成边界、resume/clone/steer/follow-up、queue-safe abort、typed state/stats、动态 model/thinking、长操作 compact | Complete（0.84.3+ session-setting 版本证明；旧版写能力 fail closed） | `pbatch/tui_adapter_pi_rpc.py`、`pbatch/tui_pi_controls.py`、`pbatch/pi_version.py`、`tests/test_pi_rpc_controls.py`、`tests/test_tui_model_controls.py` |
| TUI command surface | immutable registry 同源生成 help/dispatch/palette/busy policy；slash 名称补全纯本地；model/thinking 每 adapter/session/kind 上下文一次只读快照、本地 fuzzy、只填入后再提交 | Complete（cycle、`/scoped-models` 仍等待权威 scope 协议；available catalog 不冒充 scopedModels） | `pbatch/tui_command_specs.py`、`pbatch/tui_composer.py`、`pbatch/tui_runtime_completion.py`、`tests/test_tui_command_surface.py`、`tests/test_tui_dynamic_completion.py` |
| Provider-neutral Python session API | 同步 `AgentSession` facade、能力检查、typed 终态/队列结果/state/stats/compaction、Pi 默认 RPC 路由 | Complete（非官方 Pi SDK port；持久设置显式 opt-in） | `pbatch/agent_session.py`、`tests/test_agent_session.py` |

完成状态要求实现、文档和可执行证据同时存在。候选想法统一进入
`docs/deferred-backlog.md`，具备验收标准后再提升。

官方 Node/TypeScript SDK `@earendil-works/pi-coding-agent` 属于上游接口，不登记为
本仓库已实现能力。Node 应直接使用其 `createAgentSession()` /
`createAgentSessionRuntime()`；本仓库 Python API 不宣称与其类型或事件 wire-compatible。
