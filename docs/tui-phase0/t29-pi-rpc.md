# T29 · Pi canonical 调用面与 RPC Runtime

本阶段的唯一外部基准是
[`earendil-works/pi`](https://github.com/earendil-works/pi)。pi-batch 不再从
Codex CLI 反推 Pi 的交互或会话语义，也不把一次一进程的 `pi -p` 适配器冒充成
Pi 原生会话。

## 调用矩阵，避免参数冲突

pi-batch 自身已有 `--mode serial|parallel`，而 Pi 的 `--mode` 是
`text|json|rpc`（官方用户文档以 `-p/--print` 表达一次性文本调用）。顶层复用同一个
参数会破坏既有批处理脚本，因此调用面分为：

| 入口 | 所有者 | 协议与用途 | 不包含 |
|---|---|---|---|
| `pi-batch pi ...` | 官方 Pi CLI | argv 原样进入 Pi：interactive、print、JSON events、raw RPC、auth、package、export | pi-batch journal/gate/retry/产物重写 |
| `pi-batch` / `pi-batch tui` | pi-batch 产品 TUI | journal、inspector、工程治理；agent=pi 时托管长期 Pi RPC 子进程 | 官方 Pi interactive TUI |
| `pi-batch -p ...` / pipeline | pi-batch runner | serial/parallel、validator、gate、retry | Pi CLI 参数兼容承诺 |
| `pbatch.create_agent_session(...)` | pi-batch Python API | 同步 provider-neutral `AgentRuntime` facade；Pi 默认走 RPC | 官方 Pi SDK 的 Python port/binding |
| `@earendil-works/pi-coding-agent` | 官方 Pi Node/TypeScript SDK | `createAgentSession()` 直接嵌入；高级 session replacement 使用 `createAgentSessionRuntime()` | pi-batch Python API 或 TUI journal |

`pi-batch pi` 之后的参数不经 pi-batch 解释，也不交给当前配置中的 Codex、Claude
或其他 adapter。它只会启动 Pi executable，继承 cwd、stdin、stdout、stderr 和
环境。选择顺序为：非空 `PBATCH_PI_BIN`、basename 恰为 `pi` 的
`agent.bin`、最后是 `PATH` 中的 `pi`；非 Pi agent 配置不会接收这些参数。

这里两个 `-p` 也不是相同契约：`pi-batch pi -p "任务"` 中 `-p` 是 Pi 的布尔
print flag，任务是 positional message；`pi-batch -p "任务"` 中 `-p` 接收
pi-batch 的 prompt 值并进入治理 runner。

```bash
# 官方 Pi 交互 TUI
pi-batch pi

# 官方 print / stdin / @file
pi-batch pi -p "总结这个项目"
cat README.md | pi-batch pi -p "总结输入"
pi-batch pi -p @README.md "检查文档"

# 官方 JSON event stream 与长驻 RPC
pi-batch pi --mode json "列出风险"
pi-batch pi --mode rpc --name integration --no-session

# 官方会话、模型、资源和包管理参数同样原样生效
pi-batch pi -c
pi-batch pi --session SESSION_ID
pi-batch pi --fork SESSION_ID
pi-batch pi --provider openai --model gpt-4o
pi-batch pi install npm:@scope/pi-package
```

`--mode json` 是只写 JSONL event stream；`--mode rpc` 则把 stdin/stdout 变成双向
command/response/event 协议。后者不应接自然语言 stdin，也不提供人类交互界面。

Node/TypeScript 进程应直接使用官方
`@earendil-works/pi-coding-agent` 的 `createAgentSession()`；需要 new/resume/fork/
import 等 active-session replacement 时再使用 `createAgentSessionRuntime()`。
pi-batch 不复制或伪装该 npm API。Python 调用使用另一套明确较窄的同步 facade：

```python
from pbatch import AgentSessionOptions, create_agent_session

with create_agent_session(AgentSessionOptions(
        agent="pi", runtime="pi-rpc")) as session:
    result = session.prompt("检查当前仓库")
    state = session.runtime_state()   # typed current model/thinking/queue counts
    stats = session.stats()           # typed messages/tokens/cost/context usage
    models = session.get_available_models()
    levels = session.get_available_thinking_levels()
    state = session.set_model("openai", "gpt-5.6")
    state = session.set_thinking_level("high")
    compacted = session.compact("保留决策和未完成事项")
```

它输出 pi-batch `RuntimeEvent` / `TurnResult`，不输出官方 TypeScript SDK 类型，也不
承诺两套 API 的方法或事件一一对应。

`compact()` 是有损且可能调用模型的长操作，不复用普通 RPC 的 5 秒 response timeout。
如调用方设置本地等待上限，超时会抛出携带原 `handle` 的 `CompactionTimeout`；这只表示
本地停止等待，provider 仍可能完成同一次压缩，调用方可继续 `exc.handle.wait()` / 
`result()`，不能再发第二次 compact 冒充重试。

Pi 0.84.1/0.84.2 的 RPC model/thinking setter 会写全局 settings，而 0.84.3+ 才默认
只改当前 session。RPC wire 没有版本协商，因此 adapter 在启动 RPC 前对同一解析后的
executable 执行一次有界 `--version` 探测；仅 canonical stable SemVer 0.84.3+ 自动开放
两项写 capability。旧版、预发布、超时、非零、stderr、超长或畸形输出一律 fail closed。
确实接受旧版本持久化副作用的 Python 调用方才显式开启：

```python
options = AgentSessionOptions(
    agent="pi", runtime="pi-rpc", allow_persistent_settings=True)
with create_agent_session(options) as session:
    actual = session.set_model("anthropic", "claude-sonnet-4-20250514")
    actual = session.set_thinking_level("high")  # 返回 provider clamp 后的 state
```

`get_messages` 暂不提升为公共 API：Pi 一次返回完整、provider-specific 的
`AgentMessage[]`，长会话可能在客户端做有界处理前就超过单条 RPC JSONL 的 1 MiB
传输上限。原始能力仍可通过 `pi-batch pi --mode rpc` 使用。

## TUI typed session 控制

托管 TUI 将 provider-neutral controls 暴露为：

```text
/session
/model [query]
/thinking [level]
/compact [instructions...]
```

不带参数的 `/model` 打开 fuzzy picker。`/model <query>` 先在
`get_available_models` 当前快照中做大小写不敏感的精确解析：canonical
`provider/model-id` 或没有跨 provider 歧义的唯一裸 ID 命中后，使用 catalog 中的
canonical provider/id 只修改当前 session；未命中或歧义 query 则作为 picker 的初始
模糊过滤条件。不带参数的 `/thinking` 打开当前模型动态返回的 level picker；
`/thinking <level>` 只接受 `get_available_thinking_levels` 中大小写不敏感的精确值，
并向 provider 发送列表里的 canonical level。

两类 mutation 都在 response 后读取 `get_state`；model 还会重新读取 thinking levels，
因为 Pi 切模型时会按目标模型能力重新选择或 clamp。托管 legacy JSONL RPC 没有
`persist` wire，不能实现官方原生 TUI 的 Ctrl+S 全局默认持久化；Ctrl+S 只显示不可用
提示并保持 picker 打开。旧版 Pi，或 runtime 报告 `settings_scope=global` 时，picker
仍可查询、过滤和查看当前项，但保持只读，Enter 不修改状态或关闭 picker；TUI 不启用
全局 settings 旁路。该只读边界同样适用于参数补全：旧/global runtime 可以读取
available catalog/levels 并把候选填入 composer，但后续提交仍不能进入 setter。

### Slash command discovery

composer 的命令名补全对照 Pi 的“builtin command 本地注册”边界，但不把 Pi
interactive 内部状态伪装成 JSONL RPC 能力。`/help`、slash dispatch、命令面板和
turn/compaction/setting busy allowlist 由同一个 immutable command registry 生成；
alias 也解析到同一条 spec，避免四份手写命令表漂移。输入命令名前缀后，Tab 或 Right
只补全 composer；部分名称上的第一次 Enter 也只补全，精确命令仍一次 Enter 提交。
该路径不读取文件、不调用 adapter/RPC、不修改 TUI options，也不执行命令。

`/model QUERY` 与 `/thinking QUERY` 的参数补全使用另一条显式只读路径。键盘输入进入
精确命令上下文时异步读取 `get_available_models` 或
`get_available_thinking_levels`；粘贴只登记 context，第一次 Tab 才显式开始读取。读取
按 adapter identity + local session id + kind + generation 缓存一次 immutable 快照。
随后 query 变化只在该快照上做本地 fuzzy，
不会逐键发 RPC；离开上下文、session/rebind/busy/unmount 或 model/thinking operation
都会失效快照，迟到结果静默丢弃。snapshot 加载失败后也不会因继续输入而反复请求，
只有离开并重新进入新的上下文后才重试。

completion-only picker 独立使用 Pi 的字符顺序 fuzzy：连续与词边界加权、间隔与靠后
位置惩罚、空白及 `/` 分 token 后全部命中，并保留字母/数字交换回退。model 搜索文本为
`id provider provider/id provider id name`；thinking 只搜索 level 字符串。设置 picker
仍可搜索 thinking 说明文字，但该便利不会泄漏进 Pi 参数补全的候选或排序。

补全 picker 的 Tab/Enter 只把 canonical `/model provider/id` 或 `/thinking level` 填回
composer，触发补全本身也绝不调用 setter；用户回到 composer 后第二次
Enter 才提交命令。正常 handler 会重新获取 fresh catalog/levels 并复验 busy、Pi
版本及 `settings_scope=session`，所以补全快照只是建议，不是 mutation authority。

命令面板现在同样提供 `/session`、`/model` 与 `/thinking` 的入口，但回调仍进入原有
handler，因此 busy lock、capability、Pi 版本和 `settings_scope=session` 门禁不会被
绕过。`/compact` 是有损操作，未加入一键 palette。

Pi 官方 interactive `/model` 参数补全在 `session.scopedModels` 非空时只使用该 scope，
否则才回退到 model runtime 的 available snapshot；`/thinking` 则读取当前模型的动态
levels。RPC `get_available_models` 的合同只承诺“all configured models”，command union
也没有 scoped-model list 的 get/set wire。因此本产品用 RPC full available catalog
做只读建议，但绝不把它命名或解释为 `scopedModels`，也不从中推导 cycle membership
或顺序。官方命令名是 `/scoped-models`，不是 `/models`，本产品仍不注册或模拟它。
`cycle_model` / `cycle_thinking_level` 也暂不提升为快捷键能力：它们需要独立 typed
capability、single-flight mutation 和 `get_state` readback。

上述区别固定核对 Pi commit `e86823096c5bad39e1ca282ec24bc5eb9bec745b`：
[interactive model/thinking autocomplete](https://github.com/earendil-works/pi/blob/e86823096c5bad39e1ca282ec24bc5eb9bec745b/packages/coding-agent/src/modes/interactive/interactive-mode.ts#L646-L683)、
[RPC available catalogs](https://github.com/earendil-works/pi/blob/e86823096c5bad39e1ca282ec24bc5eb9bec745b/packages/coding-agent/docs/rpc.md#L255-L327)、
[RPC command union](https://github.com/earendil-works/pi/blob/e86823096c5bad39e1ca282ec24bc5eb9bec745b/packages/coding-agent/src/modes/rpc/rpc-types.ts#L27-L35)
和 [builtin `/scoped-models`](https://github.com/earendil-works/pi/blob/e86823096c5bad39e1ca282ec24bc5eb9bec745b/packages/coding-agent/src/core/slash-commands.ts#L17-L23)。

`/session` 要求 runtime 同时声明 `session_state` 与 `session_stats`，在 Textual thread
worker 中依次调用 `get_state` / `get_session_stats`。它可在 agent turn 期间只读执行，
不会请求无界 `get_messages`，也不会把 `sessionFile` 路径显示到 transcript。回调绑定
adapter identity、local session id 和 generation，任一变化都会静默丢弃迟到快照。

`/compact` 要求 `session_compact`，只在 idle session 调用一次
`start_compaction(CompactSessionRequest)`。命令启动后立刻返回 UI，另一 worker 对同一个
handle 做无本地 timeout 的等待。它不用 turn/review 的 `_busy`，以免错误启用 steer、
follow-up 或 backend cancel；独立逻辑锁仍会阻断 turn、new/resume/fork 和 runtime
设置。Pi RPC 没有 `abort_compaction`：Ctrl+C 只报告不可安全取消，退出则关闭整个
adapter，不能把 transport close 伪装成 provider 已回滚。失败不自动重试，因为迟到的
第一次操作可能已经成功并计费。

结果仅展示 normalized summary、tokens before、estimated after、usage/cost；
`estimatedTokensAfter` 是 provider 估算，不冒充下一轮的权威 context usage。所有文本
去 ANSI/C0 control，并受 64 行、32 KiB 与终端行宽约束。

## TUI 的 Pi RPC 路由

TUI `runtime=auto` 的选择顺序是：

1. agent basename 为 `pi`：`pi-rpc`；
2. agent 为 Codex：现有 Structured Exec；
3. 其他 CLI：Legacy adapter。

也可显式运行：

```bash
pi-batch tui --agent-bin pi --runtime pi-rpc
```

`PiRpcAdapter` 为一个 TUI session 持有一个本地 stdio 子进程：

```text
pi --mode rpc [--provider P] [--model M] [--name N]
```

传输只按 LF (`\n`) 分隔 JSON 对象。每个 request 使用唯一 id；response 只与同 id
的 waiter 配对。stdout 只承载协议，stderr 是有界诊断。关闭时先关闭 stdin 并有界
等待，随后终止整个子进程组，避免遗留 provider 进程。

## 完成边界

Pi RPC 的 `prompt` response 只表示请求已接受、排队或立即处理；它不是任务成功。
`agent_end` 后还可能自动 retry、compaction 或处理队列，也不是最终边界。Runtime
只在 `agent_settled` 后，根据最后一条权威 `message_end.message` 的
`stopReason` / `errorMessage` 生成：

- `turn.completed`
- `turn.failed`
- `turn.interrupted`

`message_update` 只作为增量展示；最终正文以 `message_end` 为准。Pi 0.84.1 的
`message_update` 没有顶层 usage，较新版本可能有；缺字段不会被误判为协议失败。

## 会话与能力

- `get_state.sessionFile` 是 provider resume handle；`sessionId` 只作身份信息。
- resume 使用 `switch_session(sessionPath)`，并再次 `get_state` 精确核对。
- 当前 pi-batch 的 provider-level fork 操作映射为 Pi `clone`（复制 active branch）；
  Pi 原生按 entry fork/tree 操作可通过 `pi-batch pi` 完整使用。
- Pi RPC 的 steer、follow-up 与 abort 均按显式 command 调用；不从 transcript 正文
  识别控制指令。
- state/stats 只返回归一化 DTO；model 中的 base URL、headers、compat 等 provider
  原始字段不会泄露到公共 API。手动 compact 要求 session idle。
- Pi 0.84.1 没有 RPC `clear_queue`；当 `pendingMessageCount > 0` 时，adapter 会拒绝
  把 abort 冒充“全部取消”。无 pending queue 时 abort 在后台等 Pi 真正 idle，
  不受普通 5 秒 RPC timeout 误判。
- Pi 没有内置 filesystem/process/network 权限隔离，因此 `pi-rpc` 不宣称
  `typed_approvals`，也不暴露为网络服务。

Runtime capability 只报告当前 adapter 已接通的能力；Pi RPC 原生存在但尚未进入
provider-neutral API 的功能保持 false，不能因上游“理论支持”而提前显示为可用。

## 版本边界

state/stats 与基础 compaction wire 以本机 Pi 0.84.1 为兼容下限，并核对当前官方主线
的共同协议；托管手动 compaction 推荐 Pi 0.84.3+，因为该版本修复了 summary 达到
output token limit 时仍持久化截断摘要的问题。`clear_queue` 是
较新主线能力，0.84.1 不支持，因此不能无条件发送。较新 wire 字段按可选字段读取；
未知 provider event 只生成有界 warning，不提升为审批、gate 或成功事实。

权威资料：

- [Pi Coding Agent README / CLI Reference](https://github.com/earendil-works/pi/tree/e86823096c5bad39e1ca282ec24bc5eb9bec745b/packages/coding-agent)
- [Pi JSON event stream](https://github.com/earendil-works/pi/blob/e86823096c5bad39e1ca282ec24bc5eb9bec745b/packages/coding-agent/docs/json.md)
- [Pi RPC protocol](https://github.com/earendil-works/pi/blob/e86823096c5bad39e1ca282ec24bc5eb9bec745b/packages/coding-agent/docs/rpc.md)
- [Pi SDK](https://github.com/earendil-works/pi/blob/e86823096c5bad39e1ca282ec24bc5eb9bec745b/packages/coding-agent/docs/sdk.md)
- [Pi sessions](https://github.com/earendil-works/pi/blob/e86823096c5bad39e1ca282ec24bc5eb9bec745b/packages/coding-agent/docs/sessions.md)
- [Pi builtin slash commands](https://github.com/earendil-works/pi/blob/e86823096c5bad39e1ca282ec24bc5eb9bec745b/packages/coding-agent/src/core/slash-commands.ts)
- [Pi interactive autocomplete wiring](https://github.com/earendil-works/pi/blob/e86823096c5bad39e1ca282ec24bc5eb9bec745b/packages/coding-agent/src/modes/interactive/interactive-mode.ts)

## 验证

```bash
python -m pytest -q tests/test_pi_cli.py
python -m pytest -q tests/test_tui_adapter_pi_rpc.py
python -m pytest -q tests/test_pi_rpc_controls.py
python -m pytest -q tests/test_pi_rpc_acceptance_boundaries.py
python -m pytest -q tests/test_agent_session.py
python -m pytest -q tests/test_tui_pi_controls_commands.py
python -m pytest -q tests/test_tui_command_surface.py
python -m pytest -q tests/test_tui_wiring_regressions.py

# 不调用模型的本机协议 smoke
printf '%s\n' '{"id":"smoke","type":"get_state"}' \
  | pi --mode rpc --no-session --offline
```

完整发布仍以 `make ci` 为准。
