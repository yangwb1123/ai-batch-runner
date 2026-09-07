# T22：Agents Inspector

状态：已交付当前 operation、共享终态账本与可信 active-turn Actor Tree 的独立只读视图。

## 正向流程

1. 用户运行 `/agents [agent]`、兼容别名 `/runs [agent]`，或从命令面板打开视图。
2. TUI 从当前 resolved cwd 对应的 `data_dir` 读取共享 agent ledger。
3. 若当前 TUI 正在执行 turn/review，在列表顶部加入一个明确标记为 `current` 的
   coordinator operation；该行只来自本进程生命周期和可信 runtime projection。
4. 若 runtime projection 提供可信 child lineage，在账本列表上方展示当前 turn 的
   Actor Tree；上下键仍选择账本记录，查看 agent/provider、run/session、attempt、
   elapsed、exit code、任务摘要和有界产物路径引用。
5. `a` 轮换 agent 过滤，`s` 轮换 status 过滤，`r` 重新读取账本，Esc 关闭。

整个流程不向 conversation transcript 添加文本，也不读取产物正文。

## 有界与失败关闭合同

- 视图最多展示 100 条；共享账本总量 16 MiB，读取单行上限 4 KiB。
- 账本任务摘要、路径长度和产物数量在写入时受限；详情再次限制为 8 KiB。
- 非 JSON、非对象、超长或缺少 agent 的记录不会进入列表；heading/detail 显示跳过数。
- 超过结果上限时只展示最新记录并显式标记截断；读取错误显示为 ledger error。
- 当前 operation 显示 `Exit: pending`，完成后立即移除；终态是否出现只由共享账本决定。
- ledger 记录不包含 parent actor、权限继承或 live handle；Actor Tree 只来自当前
  runtime projection 的 typed actor events，不能由 ledger 补造历史 lineage。
- child actor 的 parent/actor/run/turn 身份由 Projection 校验；`/actor-stop` 和
  `/actor-steer` 只提交当前 typed tree 中 exact actor/turn identity，缺少 runtime
  `actor_control` capability、目标过期或方法缺失时 fail closed。

## 验证

```bash
python -m pytest tests/test_agent_ledger.py tests/test_tui_agents.py -q
python quality.py pbatch/agent_ledger.py pbatch/tui_agent_runs.py
```
