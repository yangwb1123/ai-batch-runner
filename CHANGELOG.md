# CHANGELOG

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。

## [0.3.0] - 2026-08-20（交互式 TUI）

### 新增

- 无参数、真实终端下默认启动的 Textual 工作台；显式入口为 `pi-batch tui`。
- 对话式 Markdown 界面、流式 stdout/stderr、状态侧栏、命令面板、稳定会话，
  以及 `/help`、`/status`、`/new`、`/clear`、`/model`、`/provider`、
  `/agent`、`/memory`、`/doctor`、`/quit`。
- `Ctrl+P/N/L/C/Q` 键盘工作流；运行中防重复提交，取消会终止 agent 进程组。
- TUI turn 复用既有 prompt/session/memory/timeout/失败签名机制，并遵守项目
  单实例锁；缺少 agent 时首屏仍可打开并展示诊断。

### 兼容性

- stdin/stdout 任一被重定向时不进入 TUI；pipe prompt、`-p`、pipeline、所有
  子命令和 JSON 输出保持原有无头契约。
- wheel 将 `textual>=8.2,<9` 声明为核心依赖，并包含 TUI/backend 模块。

## [0.2.0] - 2026-08-08（产品化里程碑）

### 新增（产品 CLI 规整）
- **用户级工作目录（XDG 兼容）**：`~/.config/pbatch/`（配置）、
  `~/.cache/pbatch/`（缓存）、`~/.local/share/pbatch/`（数据：设备密钥/全局日志）；
  支持 `PBATCH_HOME=~/.pbatch` 统一收拢模式。
- **配置分层**：CLI 参数 > 项目 `pi-batch.yaml` > `~/.config/pbatch/settings.yaml`
  > 内置默认（节内键级合并）。
- `pi-batch init`：创建用户级目录骨架 + settings 模板。
- `pi-batch version [--json]`：版本（pyproject 单事实源）+ 环境信息。
- `pi-batch clean [--all]`：清空用户级缓存（--all 连配置/密钥，需确认）。
- `pi-batch completion bash|zsh`：shell 补全（注册表驱动，新命令自动同步）。
- `pi-batch doctor [--json]`：环境诊断（安装/目录/配置分层/agent/密钥/锁）。
- `pi-batch devices keymigrate`：旧项目内设备密钥迁移到用户级（跨项目身份复用）。

### 变更
- 设备密钥默认目录：项目内 `.pi-batch/devices/keys/` → 用户级
  （显式 `--state-dir` 仍用项目内，测试隔离不受影响）。
- 项目相关状态（会话/记忆索引/被拒产物）**保留项目内 `.pi-batch/`**——
  团队共享；用户级目录只放跨项目数据（边界文档化）。

### 新增（2026-08 campaign 实战内化）
- `org gate --log`：`{log}` 占位符注入；`_execute_check` 残留未知占位符
  **fail closed**（不再静默假通过）。
- campaign 模块发现排除打包产物（egg-info/__pycache__/dist/build/
  node_modules/.venv）。
- `repository-campaign-pipeline.yaml`：`meta_role_retries: 2` +
  `meta_max_failed_roles: 1`（provider 抖动韧性）。
- `check-knowledge-freshness`：设备清单配置假阳性修复。
- 新增命令：`probe`（Dry-run 标准输出，区间+置信度）、`replay`
  （决策重放：胶囊版本对比 + eval 回归）、`test-report`（测试分类统计）。
- `devices clock-skew`（时钟纪律）、`serve --device-quota`（per-device 并发上限）。
- `pi-batch check` 聚合 8 门禁（quality/registry/org/capabilities/graph/
  tools/knowledge/eval）。
- pytest markers（unit/contract/property/scenario/chaos）+ 代表性打标。

### 修复
- merge origin/main（使用反馈 P1-P7）：P2 容忍语义移植 cli_batch、
  P3 `_record_fatal_exit` 移植拆分版 cli.py、P1 from_dir aggregate。
- cli.py 拆分版恢复（392 行 re-export）+ 行数预算归位。
- `project verify` 索引不一致修复（ADR 库重索引）。

## [0.1.0] - 2026-08-06（能力里程碑）

- 74 命令（49 顶层 + 25 嵌套）、1086+ 回归测试、42 eval 用例。
- 理论层（P1-P10）/ 运行时层 / 治理层（truth/causal/temporal/…）/
  设备织网（D0-D6：SSH 受控执行、Runner、控制平面、调度/迁移/检查点/投机执行）。
- 工程知识循环（impact/adr/project memory）、Reflection 引擎、
  17 节点 AI 软件工程组织、41 角色模板。
- 规则系统（双域注册表 + 双向校验 + learn 生命周期 + eval 回归）。
