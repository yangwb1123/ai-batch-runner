# pi-batch TUI 产品契约

`pi-batch` 的交互界面是现有执行内核的产品入口，不是第二套 runner。
批处理、流水线、验证器、会话、记忆、超时和进程组清理仍由 `pbatch`
现有模块负责；TUI 只负责输入、状态呈现和用户控制。

## 启动契约

- `pi-batch`：仅当没有参数且 stdin/stdout 都是 TTY 时进入交互界面。
- `pi-batch tui`：显式进入交互界面；非 TTY 环境 fail closed（exit 2）。
- `pi-batch -p ...`、stdin pipe、所有子命令和 `--json` 输出保持脚本兼容，
  不输出全屏控制序列。
- 首屏是只读的。未安装或未配置 agent 时仍能打开，第一次提交时给出可操作诊断。

## MVP 用户旅程

1. 打开当前 workspace，查看 Git、agent、model/provider、memory 和 session 状态。
2. 在底部 composer 输入自然语言任务；一个会话内连续提问复用同一 agent session。
3. 模型正文实时进入对话区，stderr 和生命周期信息以独立状态呈现。
4. 运行中禁止重复提交；用户可取消当前 turn，取消必须终止 agent 进程组。
5. 完成或失败后回到可输入状态，可继续、切换新会话或查看诊断。

命令面板和 slash 命令提供同一组高频动作：帮助、状态、新会话、清屏、
agent/model/provider/memory 设置、诊断和退出。未知 slash 命令不会作为 prompt
发送给 agent。

## 信任边界

- TUI 不自动批准 pipeline approval，不设置绕过确认的环境变量。
- 不自动 commit、merge、创建 worktree 或调用远程设备；这些能力仍通过显式
  CLI/pipeline 契约启用。
- agent 输出、stderr 和历史都是不可信内容；界面只渲染，不把它们解释成命令。
- 取消、超时和退出必须恢复终端，并清理当前 agent 进程组。
- 运行时数据继续遵循 `--data-dir` / `PBATCH_DATA_DIR` 和 memory scope，不新建
  TUI 私有的无边界历史库。

## 发布门禁

首个产品版本至少满足：

- 无参数 TTY、显式 `tui`、pipe stdin、`--help`/`--version` 四类入口互不污染；
- fake agent 验证真实增量输出、稳定 session、失败恢复和取消；
- Textual 无头测试覆盖提交、busy 防重入、slash 命令和窄终端；
- wheel 声明并安装 TUI 运行依赖，fresh install 后可从源码目录之外启动；
- 现有完整 `make ci` 保持通过。

## 后续版本

- 结构化 run/stage/task 时间线与可恢复历史；
- 执行前 plan JSON（读取/写入范围、validators、gates、预算、远端副作用）；
- run-scoped approval IPC，而不是模拟 stdin 确认；
- artifact/diff 审阅、Campaign/Isolate 生命周期和设备 Fabric 视图；
- 机器可读 CommandSpec（参数、风险、前置条件、输出 schema），替代为 59 个
  子命令手写表单。
