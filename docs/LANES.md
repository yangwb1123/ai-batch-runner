# 泳道循环：并发实现、门禁合并与监督窗口

`pi-batch lanes` 是“多路并发实现 → 门禁 → 合并回主分支 → 记账 → 继续”的操作入口。
它只做编排，不决定目标、不选模型、不放宽门禁：泳道命令由计划文件声明，
窗口与调用预算由命令行声明一次。

```bash
pi-batch lanes status --json                 # 只读：锚定窗口、轮次、恢复次数、停止原因
pi-batch lanes run --plan lanes.yaml --dry-run
pi-batch lanes run --plan lanes.yaml --hours 12 --invocations 40 --max-rounds 0
```

## 1. 计划文件

```yaml
gate: "make ci"          # 每次合并前在 rebase 后的泳道 worktree 里执行
lanes:
  - id: progress
    branch: lane/progress
    worktree: /var/tmp/lane-progress
    command: ["pi-batch", "/abs/task.yaml", "--model", "openai-codex/gpt-6.1-sol",
              "--thinking", "max", "--git-commit"]
    scope: ["pbatch/progress_*.py", "tests/test_progress_*.py"]   # 允许改动的路径
    timeout: 1800
```

- 每条泳道一个分支、一个仓库外 worktree、一条 argv 命令；命令在 worktree 内执行。
- 泳道命令必须**自己提交**（例如 `--git-commit`）：未提交的泳道不会被合并。
- `scope` 声明该泳道允许改动的路径（精确路径、目录前缀或 glob）。分支相对 base
  改到范围外的文件即 `lane_scope_violation`，**不合并**并保留现场；范围守卫在
  门禁之前执行，绿灯门禁不能拓宽声明范围。
- 计划校验失败（重复 id/分支/worktree、worktree 在仓库内或非空、命令为空、
  未知字段、缺 gate）在创建任何 worktree 之前拒绝。
- 主工作区必须是干净的：合并事务拒绝在脏根仓库上运行。
- gate **按泳道**在各自 rebase 后的 worktree 里执行，所以它必须与“哪条泳道”无关：
  `make ci`、`pytest -q`、`test -f base.txt` 都可以；
  `test -f alpha.txt -a -f beta.txt` 会让第一条泳道必然失败（实测如此）。
  需要“每条泳道自己的门禁”时，先把它写进泳道命令，或等待后续版本。

## 2. 一轮发生什么

1. 并发执行所有泳道命令（`--jobs`，最多 8 条）。
2. 通过的泳道按声明顺序**串行**合并：rebase 到当前 `--base` → 跑 gate →
   合并 → 清理 worktree 与分支。第二条泳道因此基于第一条的结果，
   不会合并过期树。
3. 失败/超时/未提交/门禁红的泳道**不合并**，保留 worktree 供诊断。
4. 整轮写一条 `lanes/rounds.jsonl` 收据（每条泳道状态、合并提交、HEAD）。

## 3. 窗口与重启（`pbatch/lane_loop.py`）

- 调用预算按**已启动的泳道**计数：失败的泳道同样花钱，同样扣预算。
- 12 小时窗口和调用预算在**第一次运行前**写入
  `<data_dir>/lanes/supervision.jsonl`；重启复用，不延长、不重置。
- 只有“泳道命令消失且没有终态收据”才判定为崩溃并重试：退避 30/60/120 秒，
  最多 3 次。
- **门禁失败、阻塞、预算耗尽、截止时间到达 → 停止**，并记录原因。
  重试确定性失败只会掩盖它并继续花钱。
- 账本损坏或窗口参数被改写 → 拒绝继续，不悄悄开新窗口。

## 3.1 门禁失败先隔离复跑（策略 b）

门禁在 `rebase` 后执行整套测试；环境抖动（并发写锁、PTY）会让与改动无关的测试
偶发失败。判定规则：

1. 从门禁输出解析 `FAILED <node id>`；没有测试 ID → **不重试**，按确定性失败阻断。
2. 在该泳道 worktree 里**只**重跑这些测试；全部通过才判定为抖动。
3. 抖动成立时**重试门禁一次**（最多一次）；仍失败即阻断。
4. 原始失败、隔离复跑结果和重试次数都写入泳道收据（`flake_evidence`、
   `merge_attempts`、`merge_tail`）。

隔离复跑失败 → 真实回归，不重试。策略不隐藏失败：每次尝试都留证。

## 4. 明确不做

- 不选模型、不写 prompt、不改预算；这些属于计划文件与操作者。
- 不做推送、发布、部署：合并只发生在本地分支。
- 不把“进程还活着”当成进展，不把“合并成功”当成目标验收。
- 不自动修复失败泳道，也不清理他人 worktree。

## 5. 验收边界

`tests/test_lane_loop.py`、`tests/test_lane_run.py`、`tests/test_lane_cli.py`
覆盖窗口锚定、重启策略、并发执行、串行门禁合并、脏仓库拒绝、计划校验与
收据。它们使用临时 git 仓库与假泳道命令，**不**证明真实模型、真实门禁
或 12 小时长跑已经验收。
