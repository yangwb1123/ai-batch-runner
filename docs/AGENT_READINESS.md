# Agent readiness (operator-declared probe)

`pi-batch` 不猜测任何 CLI agent 的登录/认证状态，也不调用模型判断 agent 是否
可用。**Readiness 是运维声明的本地命令契约**：命令退出码回答“该 agent 现在
能不能开始干活”。

这补的是“二进制存在 ≠ 已登录”的缺口：`config.agent_preflight_errors` 只检查
可执行文件，七天无人值守的 campaign 仍可能把预算花在未认证的 agent 上。

## 1. 配置

项目 `pi-batch.yaml` 或用户 settings（优先级：项目 > 用户 > 内置默认）：

```yaml
agent:
  # 所有 agent 共用；也可只给单个 agent 配置
  readiness_cmd: "pi auth check --json"
  agents:
    codex:
      readiness_cmd: ["codex", "login", "status"]
```

- 值可以是字符串或 argv 列表。字符串用 `shlex.split` 拆成 argv，
  **不经过 shell**：管道、重定向、`$VAR`、反引号都只是普通 argv 字面量。
- 单 agent 配置（`agent.agents.<bin>.readiness_cmd`）覆盖全局
  `agent.readiness_cmd`；可按可执行文件名或完整路径匹配（沿用
  `argument_flags` 的同一套 section 解析）。
- 不配置即为 `unknown`（见第 2 节），不是 ready，也不是错误。
- `pi-batch config check` 会校验全局及所有单 agent 的 `readiness_cmd` 形状，
  包括被覆盖或未选中的声明；非法配置作为 validation error 报告，JSON 中
  `valid=false`，命令非零退出。此检查只解析配置，**不启动探针、不检查命令
  是否存在或认证是否有效**；合法形状不代表 agent 已 ready。
- 非法配置包括空字符串/列表、非字符串元素、无法解析的引号、NUL 字节、
  字符串超过 4096 字符、argv 超过 32 项或单项超过 256 字符。
  探针解析仍保留同样的防御校验：非法声明一律 `unknown`，不会退回其他命令、
  不会执行、不会当作 ready。
- 本版本没有 timeout/cap 配置项，也没有跳过 readiness 的开关；预算内固定
  10 秒硬超时、8 KiB 输出上限。

示例（仅作形状参考，命令是否存在取决于本机安装；产品代码不内置任何厂商命令）：

```yaml
agent:
  agents:
    codex:
      readiness_cmd: "codex login status"          # 未登录时非零退出
    pi:
      readiness_cmd: ["pi", "auth", "check", "--json"]
```

## 2. 分类（精确三态）

| 观测 | 结果 |
|---|---|
| 探针退出码 `0` | `ready` |
| 探针退出码非零（`1`、`2`…，包括未登录/未认证） | `not_ready` |
| 未配置；配置非法；无法 spawn；超时；输出超过上限；被信号终止 | `unknown` |

- `ready`/`not_ready` 只看退出码，不解析正文。因此探针命令必须自行把
  “可工作/不可工作”映射为退出状态；`0` 即使输出 “not logged in” 也判
  `ready`。这是显式的运维契约，不是产品内置的厂商语义。
- `unknown` 永远不等于 `ready`：`ready` 只能由一次真实退出码 `0` 得到。
- 未配置/无法测量时 JSON 中的 `exit_code`、`duration_ms`、`diagnostic` 为
  `null`，不补造 `0`。

## 3. 有界与脱敏

- 硬超时覆盖整个探针执行窗口（默认 10 秒），超时即杀进程组并判 `unknown`。
- 只保留有界输出（8 KiB 上限）；超出上限判 `unknown`，不把截断输出当作成功。
- 诊断信息只暴露 stderr/stdout 的**尾部**（最多 400 字符），控制字符折叠为空格。
- 常见凭据形状（`token=...`、`password: ...`、`--api-key ...`、`Authorization:`
  /`Bearer ...` 等）在展示前替换为 `<redacted>`。这是显示层的最佳努力过滤，
  不是凭据扫描器；探针输出由运维命令产生，`pi-batch` **不读取凭据文件**、
不把探针输出写入 ledger，也不记录完整正文。
- 探针只执行一次声明的 argv；不追加 model/provider/thinking 参数，不调用模型。
  探针继承当前进程环境（以便读取登录状态）；`pi-batch` 不读取、不记录这些
  环境变量。

## 4. 只读查询

```bash
pi-batch config check --json              # 仅校验配置形状，不运行探针
pi-batch agents readiness                 # 默认 5 个已知 agent
pi-batch agents readiness --agent codex   # 单个
pi-batch agents readiness --json          # 机器可读
```

- 这是只读观测：不写 ledger、不创建运行目录、不改变 campaign 状态；退出码
  为 `0` 表示查询本身完成，`status` 字段才是 readiness 结论。脚本若要以
  readiness 作为门禁，应解析 JSON，而不是复用查询退出码。
- 每行 JSON 固定字段：`agent`、`status`、`configured`、`source`、`command`、
  `exit_code`、`timed_out`、`duration_ms`、`reason`、`diagnostic`；字符串均有界。
- `--agent` 只接受 1–128 个非空白、非控制字符；否则参数错误退出。
  默认查询 5 个 agent，逐个串行执行（最多 5 × 10 秒），不并发拉起探针进程。
- 既有 `pi-batch agents list` 的 row 字典和表格列**保持不变**；
  readiness 是独立的子命令，不是 health 行的扩列。

## doctor 输出

`pi-batch doctor` 的文本输出在 agent 二进制检查后，逐行展示默认 5 个 agent
（pi、codex、claude、gemini、opencode）的 `ready` / `not_ready` / `unknown` 状态。
非 `ready` 时附带脱敏后的原因；未配置探针显示 `unknown`。这些 `[INFO]` 行仅供
参考，不改变既有检查及退出码：readiness 问题本身不会使 doctor 失败。
`doctor --json` 保持原有输出，不运行 readiness 探针。

## 5. Campaign 准入

`campaign` 在模块分析、模型调用和 pipeline spawn **之前**检查解析后的
`--agent-bin`：

- `not_ready`：追加一条有界 campaign 事件（`status=AGENT_NOT_READY`，
  evidence 含 agent、来源键、脱敏后的命令、退出码、诊断尾部），重写
  summary，记录 `CAMPAIGN refused before analysis: ...` 并以非零退出（与
  既有 provider 阻断相同的 `1`）。该轮不会产生任何分析任务或 pipeline。
- `unknown`：记录**一次** warning 后继续，不阻塞、不新增绕过参数。多轮
  campaign 在每一轮的准入检查处至多记录一次，不按 pipeline 重复刷屏。
- `ready`：不产生额外事件，campaign 行为与未引入 readiness 时一致。
- 顺序：campaign 预算门禁 → provider breaker 准入 → readiness → validator
  preflight/分析。既有预算阻断（`3`）、provider 暂停（`1`）、串行/隔离
  执行、失败退出码均不变。

## 6. 边界与限制

- 探针是**运维自担**的本地命令：产品不内置 `codex login status`、`pi auth
  check` 或其他厂商探测；误配一个恒返回 `0` 的命令会得到 `ready`，这是
  配置责任，不是认证证明。
- `ready` 只证明“该命令此刻退出 `0`”，不证明会话、配额、模型或网络在
  整个七天窗口内持续可用；运行期仍受 provider breaker、预算与失败停机约束。
- 探针输出是运维命令产生的不可信文本；脱敏是模式匹配，可能漏掉未知格式的
  秘密，也可能误伤普通单词。不要把凭据主动放进探针命令参数或输出。
- 该能力不改变 `agent_preflight_errors`（可执行文件存在性）、不改变 ledger
  记录、不提供自动登录/修复、不授权任何外部通知或发布。
- 测试：`tests/test_agent_readiness.py`（分类矩阵、配置失败、`config check`
  形状校验且零 spawn、无模型调用、超时/输出上限、脱敏、JSON 形状）与
  `tests/test_campaign_readiness.py`
  （拒绝先于分析且零 spawn、事件记录、unknown warning、未配置兼容、
  provider/预算优先级）。静态/单元测试通过不代表真实账号或生产验收。
