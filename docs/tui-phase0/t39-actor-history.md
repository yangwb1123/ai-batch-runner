# T39 · Retained Actor Tree history

`/agents` 现在除了当前 turn 的 Actor Tree，还能查看当前 TUI runtime
projection 保留的历史 turn：provider child、pi-batch meta role、Pipeline
stage/task 等都按可信 `actor.spawned/status/completed` 事件中的
`workspace/session/run/turn/actor/parent_actor` 绑定归组。

历史视图的边界：

- 只读取已接受的 `RuntimeEvent` projection，不读取 Assistant 正文、stderr、
  共享运行账本摘要或 item id 来推断 actor；
- 当前 turn 不重复进入 history，历史每个 turn 最多 64 个 actor、最多保留 32
  个 turn，并限制扫描总量；projection 淘汰或输入异常会显式报告 truncation/skipped；
- 每个历史组保留 workspace/session/run/turn、终态和最后事件时间，节点继续使用
  parent lineage 生成 cycle-safe tree；显式 provider `actorAuthority` 仅作为
  非权威展示元数据，继承声明必须绑定 exact parent，并受已知父级 capability/scope
  限制，绝不成为 execution grant；
- `/agents` 中按 `h` 在当前树和历史树之间循环，查看历史是只读操作，不产生
  provider 控制请求；
- `/actor-stop <actor-id>` 和 `/actor-steer <actor-id> <text>` 只从当前
  typed tree 取目标，并绑定 exact 当前 turn/actor identity；缺少显式
  `actor_control` capability、目标过期或 runtime 未实现方法时 fail closed，
  不会从历史摘要合成控制目标。provider authority 仍只是展示元数据。

历史来自内存中的 bounded projection；journal 另有只读 `checkpoint.json` 完整性见证，
但尚未用 state snapshot 跳过 replay。跨进程 provider archive 关联仍属于后续恢复层；
provider 根 authority 已接入，但仍只是展示元数据。
