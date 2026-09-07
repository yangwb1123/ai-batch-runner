# TUI + Pi/Codex 核心闭环 · 使用指南（5 分钟上手）

> `pi-batch` 可直接在真实终端使用。默认 agent=pi 时走官方 Pi RPC；agent=codex 时，
> 对话、流式正文、结构化时间线、取消、会话延续已构成可用核心闭环；
> Legacy 与 Structured Exec 的可信事件已统一通过 EventBus/Projection 驱动界面，
> 并在进入可见投影前写入按 data_dir/memory scope 隔离的有界 journal。这不代表
> 自动启动恢复、provider 持续状态订阅或完整 Phase 1 已经完成；动作前
> `thread/read` 对账、`thread/list` advisory、App Server 主 TUI/typed approval、
> 键盘 session picker、Codex 精确续跑/fork、可逆本地 archive 和独立
> Plan/Changes/Agents/Completion/Artifact Inspector 已可用；活动 turn 的进程内 follow-up
> queue 也已接通。

## 启动

```bash
# 在任意真实终端（SSH 亦可，宽度 ≥72 最佳）
cd /home/u1/ai-batch-runner
pi-batch                    # 或 python pi-batch.py（源码目录）
```

- 首屏下方 `Ask pi-batch…` 输入框直接输入任务（默认 agent 由 pi-batch.yaml 决定；
  先 `/agent codex` 或 `/agent pi` 切换执行器）。
- `pi-batch tui --runtime pi-rpc` 显式启用长期 `pi --mode rpc` runtime；`auto`
  在 agent basename 为 `pi` 时自动选择。需要官方 Pi 原生 TUI、JSON/RPC、auth、
  package 或 export 时，使用 `pi-batch pi ...` 原样透传入口。
- `/agent` 在空闲时会重绑下一轮运行时并开启 runtime-local 新会话；Codex
  Structured Exec 支持 `/model`，但不接受 `/provider` 覆盖，切换时会 fail-fast。
- `pi-batch tui --runtime app-server` 显式启用 Codex App Server；也可在空闲时运行
  `/runtime app-server`。握手失败会阻断执行，不会假装成 typed runtime。
- 非 TTY / 管道环境不会进入 TUI（保持无头契约）。

## 官方 Pi 调用面

```bash
pi-batch pi                              # 官方 Pi TUI
pi-batch pi --print "总结仓库"            # one-shot text；prompt 是 positional
cat README.md | pi-batch pi -p "总结输入" # print 会合并 piped stdin
pi-batch pi --mode json "检查风险"       # JSONL events
pi-batch pi --mode rpc --no-session      # raw LF-delimited JSONL RPC
pi-batch pi --session PATH_OR_ID         # 官方会话恢复
pi-batch pi install npm:@scope/pi-package # 官方 package command
```

这里 `pi` 后的参数完全由
[`earendil-works/pi`](https://github.com/earendil-works/pi) 解释；pi-batch
不把自身的 `--mode serial|parallel` 语义混入其中，也不增加 validator、retry、
journal 或完成裁决。Pi 的 `--mode json` 输出事件；`--mode rpc` 的 stdin/stdout
是协议通道，不是自然语言 prompt 通道。需要带本产品 Timeline、journal 和 Inspector
的界面时运行 `pi-batch tui --runtime pi-rpc`，不要直接操作 raw RPC。

几个相似名字的边界如下：

| 调用 | 实际接口 |
|---|---|
| `pi-batch pi -p "任务"` | Pi 官方 CLI；`-p` 是布尔 print flag，`"任务"` 是 positional message |
| `pi-batch -p "任务"` | pi-batch 治理批处理；`-p` 直接接收 prompt 值 |
| `pi-batch tui --runtime pi-rpc` | pi-batch 托管 TUI，通过长期本地 Pi RPC 子进程执行 |
| `pbatch.create_agent_session(...)` | pi-batch 同步 Python API；provider-neutral，选择 Pi 时默认走 RPC |
| `@earendil-works/pi-coding-agent` | Pi 官方 Node/TypeScript in-process SDK，不由 pi-batch 包装 |

Python 调用示例：

```python
from pbatch import AgentSessionOptions, create_agent_session

options = AgentSessionOptions(agent="pi", runtime="pi-rpc")
with create_agent_session(options) as session:
    result = session.prompt("检查当前仓库的风险")
    print(result.text)
    print(session.runtime_state())
    print(session.stats())
    print(session.get_available_models())
    print(session.get_available_thinking_levels())
    state = session.set_model("openai", "gpt-5.6")
    state = session.set_thinking_level("high")
    compacted = session.compact("保留结论、证据和待办")
```

`session.compact()` 是有损长操作，默认等 provider 完成；传 `timeout=` 只限制本地
等待，`CompactionTimeout.handle` 仍代表同一次进行中的压缩。adapter 会对同一 Pi
executable 执行有界 `--version` 探测：稳定版 0.84.3+ 自动开放 session-only setter；
旧版、预发布或无法验证的版本保持关闭。只有 Python 调用方明确接受旧版全局 settings
副作用时，才设置 `allow_persistent_settings=True`。

托管 TUI 中可直接使用同一组 typed 控制：

```text
/session
/model
/model openai/gpt-5.6
/model gpt-5.6
/thinking
/thinking high
/compact
/compact 保留架构决策、失败证据和未完成事项
```

不带参数的 `/model` 打开 fuzzy model picker；`/model <query>` 若大小写不敏感地
精确命中 canonical `provider/model-id`，或命中没有跨 provider 歧义的唯一裸 ID，
就用 catalog 中的 canonical 值只修改当前 session，否则把 query 带入 picker 做模糊
过滤。不带参数的 `/thinking` 打开当前模型的动态 level picker；
`/thinking <level>` 只接受动态列表中大小写不敏感的精确值，并同样只修改当前
session。设置后以 `get_state` 为权威结果；模型变化还会重新读取 thinking levels。

托管 Pi RPC wire 没有官方 TUI 的 Ctrl+S 持久化能力；按 Ctrl+S 只提示不可用并保持
picker 打开，不会伪造或旁路写入全局默认值。旧版 Pi，或 runtime 报告
`settings_scope=global` 时，model/thinking picker 仍可查询和过滤，但保持只读，按
Enter 也不修改状态或关闭 picker。`/session` 在后台读取 state + stats；活动 turn 中也可
查询，不会读取完整 message 历史或显示 provider session 文件路径。`/compact` 只允许 idle session，命令会立即
把界面控制权还给用户，模型摘要在后台完成；期间 `/session` 仍可用，但新 turn、
`/new`、resume/fork 以及 runtime/model/provider 设置会在 provider 调用前被拒绝。
它不占用 turn 的 `_busy`，所以 Ctrl+C 不会误取消其他 backend 工作。Pi RPC 当前没有
`abort_compaction`，界面不会谎称 Ctrl+C 已取消压缩；退出会关闭整个 adapter，最终
状态视为 unknown。迟到结果必须仍匹配原 adapter + local session + generation 才显示。

Pi 0.84.1 已具备上述基础 RPC wire；实际使用托管 `/compact` 推荐升级到 0.84.3+，
因为上游 0.84.3 修复了摘要触及 output token limit 时持久化截断内容的问题。

Node/TypeScript 应安装官方包并直接调用其 SDK：

```typescript
import { createAgentSession } from "@earendil-works/pi-coding-agent";

const { session } = await createAgentSession();
try {
  await session.prompt("检查当前仓库的风险");
} finally {
  session.dispose();
}
```

Python API 复用的是 pi-batch `AgentRuntime` 能力集合，方法名、事件类型和 session
replacement 语义不承诺与官方 TypeScript `AgentSession` 一致。

## 核心闭环演示（agent = codex）

```text
You:  /agent codex                      ← 切到结构化执行器
You:  运行 pytest tests/test_tui_timeline.py 并总结结果
      [codex 执行：正文流式出现；命令/文件/Plan/Todo/usage 进入时间线]
      → 按 Ctrl+T 展开时间线卡片：command/file/plan/usage 逐项可见；
        同一 Plan item 原位更新，成功命令折叠
You:  刚才那次运行里 FileChange 涉及哪些文件？       ← 第二轮
      [第二轮自动 resume 同一 codex 线程（thread id 已捕获），模型记得上一轮]
      → Ctrl+C 可随时中断；/status 看状态；/changes 打开独立工作区检查器，
        turn 结束提示给出本轮权威 change summary
      → 若上一轮仍在运行，直接输入会进入 follow-up queue；/queue 可查看和编辑，
        成功后自动续发，失败或取消后保留并暂停
```

## 已可用能力（agent=codex）

| 能力 | 入口 | 状态 |
|---|---|---|
| 对话 turn（含流式正文） | 输入框 | ✅ |
| 结构化时间线（command/file/plan/usage 卡片） | `Ctrl+T` / `/timeline` | ✅ |
| Plan/Todo Timeline（同 `item_id` 原位更新） | summary / `Ctrl+T` expanded | ✅ 有界投影 |
| 独立 Plan Inspector | `/plan` / 命令面板 | ✅ 同源完整有界步骤、打开期间刷新 |
| 独立 Agents Inspector | `/agents [agent]` / `/runs` | ✅ 当前 operation + active-turn typed Actor Tree + scoped 最近终态 |
| 独立 Completion Inspector | `/completion` / 命令面板 | ✅ typed facts + 权威 Git delta、活动中刷新 |
| 独立 Artifact Inspector | `/artifacts [N]`、Completion 内 `a` | ✅ typed refs + workspace/data scope 64 KiB no-follow 文本预览 |
| 命令大输出 ArtifactRef | 自动 | ✅ Codex aggregate/delta → 私有 data_dir bounded ring |
| Conversation/Status/Timeline/workspace 统一事件投影 | 自动 | ✅ 当前进程 + scoped journal |
| 跨轮会话延续（上下文记忆） | 自动（provider thread resume） | ✅ 本轮接通 |
| 退出重开后发现/恢复会话 | `/sessions`、`/resume <session-id>` | ✅ byte-bound 摘要、App Server advisory、scope 隔离 |
| provider 上下文分叉 | picker `f`、`/fork [session-id]` | ✅ capability 驱动、失败关闭 |
| 本地归档/恢复 | picker `a`、`/archive`、`/unarchive` | ✅ 可逆、不删除事件 |
| Provider 归档/恢复 | `/provider-archive <session-id>`、`/provider-unarchive <session-id>` | ✅ App Server capability + exact handle；不改本地 marker |
| 中断/取消（handle-scoped） | `Ctrl+C` | ✅ |
| 活动 turn 后续消息队列 | 普通输入、`/queue` | ✅ 进程内、有界、成功后续发 |
| 活动 turn steering | `/steer <text>` | ⚠️ 仅 runtime capability + live handle 均可用时 |
| Typed approval | 自动弹窗 / `/approvals` | ✅ App Server；a/s/d/i，Enter 默认拒绝 |
| 退出（活动 turn 会先请求取消） | `Ctrl+Q`；空闲时也可 `/quit` | ✅ |
| 工作区变化与 diff | `Ctrl+G` / `/changes [scope] [path]` | ✅ scope、文件与 hunk 导航 |
| transcript 兼容输出 | `/files` / `/diff [path]` | ✅ |
| 只读 AI Review | `/review [scope]`、`/findings`（选中后 `f`） | ✅ 报告、结构化 finding、普通 turn 修复请求与 Changes 定位 |
| Native Pipeline | `/pipeline <file> [--reuse]`；启动参数 `tui --pipeline FILE` | ✅ typed Pipeline/Stage/Task/Gate 投影；只读 attach；approval IPC |
| 产品方向提案 | `/product [requirement]` | ✅（本地生成，不启动 agent） |
| 状态/诊断 | `/status` `/doctor` `/refresh` | ✅ |
| Typed Pi session 状态与统计 | `/session` | ✅ capability 驱动、后台查询、active turn 可读 |
| Typed Pi 手动压缩 | `/compact [instructions]` | ✅ idle-only、后台等待、不可伪装取消 |
| 记忆/会话控制 | `/memory` `/new` `/clear` | ✅ |
| 窄屏降级 | 宽 <72 隐藏侧栏/时间线 | ✅ |
| 长会话 transcript 容量治理 | 自动 | ✅ 400 条 / 512 KiB，总量超限提示省略 |

## 尚未接线（诚实清单）

- **Native Pipeline provider stop/恢复**：TUI 已可通过 `/pipeline <file>` 或
  `pi-batch tui --pipeline FILE` 显式启动并观察 Pipeline；attach 只接受同 workspace、
  `pbatch-native`/`read_only` 事件，并投影可信 Pipeline/Stage/Task/Gate/Actor Tree。
  approval stage 使用精确绑定 workspace/session/run/turn/actor 的 native IPC；Ctrl+C
  使用同一 exact-run control channel 终止当前本地 task 并在 stage 边界停止后续任务
  （不发送 provider stop）；单 task actor stop 已接线。journal 现在写入有界
`checkpoint.json` 做精确边界/digest 校验，但它不跳过 replay；自动启动恢复与快照加速仍未接线。
  没有 native IPC 时仍 fail closed，需 headless `--approve-file` 或 `PBATCH_APPROVE=1`。
- **sandbox/approval policy/network 常驻有效值**：App Server 在 turn started 后将明确提供的
  effective policy 作为 typed `policy.updated` 投影到当前 turn；状态区只显示 bounded
  provider 值，缺失值保持 `unknown`，不会从本地默认值推断。其他 runtime 仍可能只显示
  unknown。
- **Structured Exec/Legacy 的 live steer**：`/steer` 控制路径已能力化；这两个 runtime
  不支持 turn 中途 steering。显式 App Server 模式已支持并校验 live handle。
- **provider/meta actor 权限与单 actor 控制**：当前 active-turn 的 typed child lineage
  已进入 `/agents` Actor Tree；App Server 仅在声明 `subagents` 且事件同时提供显式
  `actorId`/`parentActorId` 时投影 provider child，仍不从 item id 推断。retained projection
  中的 provider/meta/Pipeline actor 已按 turn 聚合为有界历史树，`h` 可在当前树与历史
  树间切换；显式 authority 的 exact-parent capability/scope 继承校验已接线。`/actor-stop
  <actor-id>` 与 `/actor-steer <actor-id> <text>` 现在只对当前 typed tree 提交 exact
  identity 请求；缺少 explicit `actor_control` capability、目标过期或 runtime 方法缺失时
  fail closed，不会根据任务摘要或历史推断控制目标。App Server 显式 root
  `actorAuthority` 现在作为只读 turn metadata 投影；仍不会转化为执行授权。
  PlanSnapshot 现在保留显式 scope、validator、gate、budget 等边界证据，但仅供
  只读展示，尚未绑定统一执行控制面。
- **退出后自动恢复旧界面/执行**：默认启动仍创建新 session；先运行 `/sessions`，
  再在 picker 中按 Enter（或用精确 `/resume <session-id>`）恢复。终态 Codex journal 且 runtime/handle/capability
  匹配时，下一 turn 续跑 provider thread；非终态、损坏、Legacy 或不匹配历史只读。
  App Server 会先用 `thread/read` 对账，只有 `notLoaded`/`idle` 状态才执行 resume；
  `active`、`systemError` 或无法验证时仍只读。旧对话不会在启动时自动选择，完整
  transcript 尚未接线。
- **Picker 中的 provider 状态**：`thread/list` 摘要是带观测时间的瞬时 advisory，
  首个 page 有 `nextCursor` 时明确显示未完成。它不会添加 remote-only thread，也不会
  决定 resume；按 Enter 后仍重新恢复 journal，并以上述 `thread/read` 结果为准。Provider
  archive/unarchive 需使用显式命令，不会由 picker 的本地 `a` 键隐式触发。
- review 入口在 headless（`pi-batch review` / `pi-batch review-fix`）与 TUI
  `/review` 均可用；TUI 默认运行独立只读 worker，支持
  `uncommitted`、`base BRANCH`、`commit SHA`、`custom TEXT` scope，并把有界报告
  写入 `docs/reviews/`。显式 App Server runtime 使用 inline `review/start`，回收
  typed finding/message evidence 并绑定当前 session/run/turn；`/findings` 的 `f` 仍启动
  普通权限受控 turn，不自动提交或解析为已修复；结束后生成
  `docs/reviews/review-fix-<id>.md`，仅 exact terminal RuntimeEvent 才关联 typed
  validation/gate/workspace evidence。finding 仍是审查意见，后续需显式重新 `/review`，
  不等同于工程门禁结果。
- headless 侧全部能力不受 TUI 影响：`-p`、pipeline、campaign、isolate 等照常。

## 常见问题

- **第二轮不记得上下文** → 检查 `/status` 里 agent 是否为 codex 且上一轮成功；
  线程失效时工具自动降级为新会话并在 stderr 提示 "resuming provider thread… /
  resume failed… starting fresh"。但用户显式执行 `/resume` 后，恢复被标为 required；
  provider 拒绝时直接失败，不会静默创建新上下文。
- **时间线看不到** → 宽 <72 自动隐藏；`/timeline` 或 Ctrl+T 切换。
- **怎样单独查看 Plan** → 使用 `/plan` 或命令面板；Timeline 仍通过 Ctrl+T
  提供紧凑摘要。Inspector 打开时会随可信 `plan.updated` 更新。
- **怎样查看当前/最近 Agent 运行** → 使用 `/agents`（`/runs` 等价）；`a` 切换
  agent、`s` 切换状态、`r` 刷新共享账本。当前操作标为 `current`，历史只显示账本
  已记录的终态和产物路径，不代表 provider 子 Agent 在线状态。
- **怎样核对这一轮究竟完成了什么** → 使用 `/completion`。它分栏展示命令、权威
  changes、未完成 Plan、validation/gate、tool、artifact、风险和 usage；没有验证事件
  就显示“未提供”，不会采信 Assistant 正文中的 PASS 表述。活动 turn 中可打开并自动刷新。
- **怎样查看明确产出的文件** → 使用 `/artifacts`，或在 Completion 中按 `a`；↑/↓ 后
  Enter 选择，`r` 重新读取。只有 typed `artifact.created` 会进入列表，`/artifacts N`
  可预选第 N 项。当前 workspace 内，或 Adapter 明确声明 `scope=data` 且落在本地解析
  data_dir 内的 UTF-8 普通文件显示前 64 KiB；远程 URI、越界路径、符号链接、目录和
  二进制内容不会读取，也不会启动外部程序。Codex 命令输出超过事件预览预算时会自动
  出现在这里；默认单文件 2 MiB，每 workspace/session 64 MiB 或 256 文件滚动保留。
- **怎样只看 staged 或某次提交** → `/changes staged`、`/changes unstaged`、
  `/changes base main` 或 `/changes commit HEAD`。Inspector 中按 1-5 切换当前工作区
  scope，↑/↓ + Enter 选择文件，`n`/`p` 切换 hunk，`r` 重新读取只读快照。
- **较早对话显示为已省略** → 当前进程的可见 transcript 有硬上限（400 条、
  512 KiB、单条 128 KiB）；当前 turn 的 assistant/error 投影各保留 64 KiB
  最新尾窗；多个 assistant message 按 `item_id` 保留并共享这一个预算，completed
  只替换对应 provisional 片段。活跃输出会持续追加，`/clear` 只清理可见内容。
- **Codex 下 `/provider` 被拒绝** → Structured Exec 当前只接受 Codex 自身 provider；
  `/model <name>` 会传给下一轮 `codex exec --model`。
- **运行时还能继续输入吗** → 可以。普通文本进入最多 16 条、总计 64 KiB 的当前
  进程队列；`/queue` 查看，`/queue edit N TEXT` 编辑，`/queue drop N` 删除。
  当前 turn 成功后自动运行队首；失败或取消后需显式 `/queue run`。队列不跨进程恢复，
  也不会随 session 切换迁移。
- **Pi 会话占用了多少上下文/费用** → 使用 `/session`。`Tokens` 是整个 session 的
  累积统计，当前上下文看 `Context`；未知值会显示 `unknown`，不会伪装成 0。
- **怎样手动压缩 Pi 上下文** → 空闲时运行 `/compact [instructions]`。这是有损摘要，
  完整 JSONL 历史仍由 Pi 保留；托管 RPC 没有单独的压缩取消命令，所以请等待完成，
  不要把本地等待或 Ctrl+C 当成 provider 已取消，也不要自动重发。
- **不需要 TUI** → 一切子命令照常无头运行（`pi-batch -p "..." --agent-bin codex`）。

## 验证入口

- `tests/test_tui_integration_codex.py`：可选真实 Codex 闭环；环境无 Codex 时跳过。
- `tests/test_tui.py`、`tests/test_tui_backend.py`、`tests/test_tui_timeline.py`、
  `tests/test_tui_transcript.py`：交互、结构化事件、Timeline 与可见容量。
- `tests/test_tui_events.py`、`tests/test_tui_controller.py`、
  `tests/test_tui_event_regressions.py`、`tests/test_tui_wiring_regressions.py`：
  canonical event、不可变 snapshot、正文隔离与权威 workspace delta。
- `tests/test_tui_journal.py`、`tests/test_tui_journal_safety_regressions.py`、
  `tests/test_tui_journal_rotation_regressions.py`：持久后可见、可信前缀恢复、
  歧义只读、身份边界、容量耗尽和 session switch 原子性。
- `tests/test_tui_session_summary.py`、`tests/test_tui_sessions.py`：byte-bound sidecar、
  stale/invalid/symlink/懒回填、scope 内 picker、provider exact join、异步 worker、
  Codex 续跑/fork、可逆 archive 和只读降级。
- `tests/test_tui_inspector.py`：Plan 同源投影、Changes scope/文件/hunk 键盘导航、
  overlay 关闭与 transcript 隔离。
- `tests/test_tui_agents.py`、`tests/test_agent_ledger.py`：Agents overlay 当前/终态投影、
  过滤刷新、容量/损坏诊断、attempt 和 transcript 隔离。
- `tests/test_tui_completion.py`：typed completion facts、正文隔离、每类容量、非有限
  usage、活动 turn 原位刷新、权威 workspace delta 和 transcript 隔离。
- `tests/test_tui_workspace.py`：staged/unstaged/untracked 分离、base/commit ref、
  路径边界和有界 diff。
- `tests/test_tui_followups.py`：FIFO/容量边界、成功续发、失败暂停、overlay 管理和
  capability-driven steer。
- `tests/test_tui_pi_controls_commands.py`：typed session/state 展示、非阻塞 compact、
  独立逻辑锁、迟到结果隔离、关闭/错误单次呈现和输出容量边界。

```bash
python -m pytest tests/test_tui_integration_codex.py -v
make ci
```
