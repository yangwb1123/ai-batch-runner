# 全局任务中心：分批实现与使用

目标：一个入口观察所有项目的目标、进度、资源消耗、交付、修复和清场。
执行事实、AI 推测、目标验收和操作授权必须分离。运行数据不进入工具源码 Git。

## 本批已实现：本机只读事实入口

```bash
# 任意目录均可查询；默认发现 PBATCH_RUNTIME_HOME/projects 下的工作空间。
pi-batch ops tasks
pi-batch ops tasks --json
pi-batch ops tasks --follow --interval 5 --json   # JSONL 快照，Ctrl-C 退出
pi-batch ops tasks --id tc-...                  # 查询当前有界窗口内的任务

# 自定义 /tmp 等运行目录明确登记一次，不扫描整机，也不重启正在运行的任务。
pi-batch ops sources add my-project \
  --data-dir /path/to/runtime \
  --workspace /path/to/project \
  --campaign-state /path/to/output/state.jsonl \
  --events-file /path/to/events.jsonl
pi-batch ops sources list --json
pi-batch ops tasks --source my-project --json
pi-batch ops sources remove src-...             # 仅移除登记，不删除任务/目录

# 浏览器与手机使用同一 JSON 投影。复用已配置的私有 token 文件。
pi-batch ops serve --global --token-file ~/.config/pbatch/ops-token
# 打开 http://127.0.0.1:8731/?token=<token>；页面每 5 秒刷新数据，不调用模型。
```

`--workspace` 只声明相对产物的定位/读取边界，不授予执行或清理权限。
`--global` 必须带 token，不能与 `--control` 组合。非 loopback 监听仍要求 TLS。
已有 `ops serve` 的工作空间模式、历史会话和控制 API 保持原作用域；默认不会扩大成全局。
远端优先通过 SSH/VPN 使用，不直接暴露 Runner。当前不是多租户 Hub 的授权聚合入口。

### 已提供的事实

- Agent 任务按来源 + run/task 身份分组，不按 prompt 标题合并。
- Campaign 按来源 + campaign/module/direction 身份展示历史与最近状态。
- 执行、验证、重试分别可追溯；metering 与 agent ledger 同一尝试去重。
- LLM 配置、调用结束后的输入/输出/缓存 token 与费用，带完整/部分/不可用标记。
- 显式开始事件或报告耗时可定位观察窗口中的时长；排队时间、ETA 暂不猜测。
- 声明产物的存在性和字节数，只检查明确登记的 workspace/runtime 边界，不读取正文。
- 分支/commit/合并记录与目标验收分开；存在文件、执行成功不等于成果已验收。
- 独立显示本机已有进程注册表的 PID 存在性与心跳，不把活动当作有效进展。
- 损坏、超长、未写完或被截断的账本窗口可见；缺失数据不当作零费用。

### 尚未实现，不能误读

这一批是对既有账本的只读投影，不会给旧任务补造历史事件。旧运行没有开始事件时，
只有调用结束后更新的账本事实和独立的进程活动视图；不声称逐 token 实时计费。
Campaign 子进程若没有可关联身份，不把整个工作空间 token 归给某一个方向。
只有短摘要时不声称分析了完整 prompt；不同项目/角色的文本不能自动成为执行授权。
历史验证未绑定当前文件完整摘要，产物标记为 `not_reverified`，不是实时 Verified。
目标验收、计划下一步和预测默认 `untracked`，单位已验收产出的成本默认未知。

API（只在 `--global` 下开放）：

```text
GET /api/tasks?limit=100&source=<已接入来源 ID 或名称>
GET /api/tasks/<task_id>?source=<已接入来源>
```

CLI/Web 共享 `schema_version: 1` 的投影。HTTP 查询不接受文件路径，不暴露原始会话正文。
接口无执行、删除、合并或上传动作；Web 用 textContent 渲染外部数据。

### 数据与读取预算

- source catalog：用户级 runtime 的 `ops/task-sources.json`，0600，锁内原子替换。
- 默认最多 64 个来源；每个 ledger 最多尾部 1 MiB / 512 条，单行最多 256 KiB。
- 单次最多返回 200 个任务、每任务 32 条观察引用、16 个产物候选。
- 进程注册表独立有界读取，不使用会创建探针或删除死记录的读取路径。
- 上述数值均是窗口观测，不是所有历史的全量统计。列表之外的任务不代表已删除。
- 读取不创建 runtime；损坏 catalog 不自动修复或覆盖；符号链接/FIFO 等不作为账本读入。

当前统一目录解析器的默认运行根为 `~/.abr`（目录变更由另一个工作批次实现）。
未显式配置运行根时，本入口还只读兼容已有 `~/.codex/pbatch` 的工作空间与进程登记。
如设置 `PBATCH_RUNTIME_HOME`、`CODEX_HOME` 或 `PBATCH_HOME`，则遵守该明确边界，
不额外读取旧默认位置；需要的旧运行目录通过 `ops sources add` 接入。
本入口不会搬迁旧数据或修改旧进程配置。config/cache/keys 的现有 XDG 路径不在本批迁移。

## 后续批次与验收条件

| 批次 | 内容 | 完成判定 |
|---|---|---|
| 2 | 目标/验收契约、持久生命周期与父子 run 关联、DAG 下一步 | 新旧事件可区分，断线恢复一致，不能以 Agent 自述判完成 |
| 3 | 完整 prompt 的授权增量意图分析、历史预测、性价比 | 明确事实/推断、来源/覆盖；质量底线优先；分析自身消耗也计费 |
| 4 | 有预算的项目修复、工具改进任务与独立验证 | 在预授权范围内产生真实代码/测试；正常门禁；不无限重试或自我授权 |
| 5 | 72 小时保留窗口、归档验证与安全清场 | 只回收登记资源；无活跃引用；未保存成果不删；历史/恢复信息仍可查询 |
| 6 | 本地最小反馈案例、去重、复现任务与发布闭环 | 默认不外发；原始 runtime 不进 Git；只晋升可复现的回归测试/修复 |
| 7 | Hub 多设备授权与 TUI/Console 共享入口 | 不绕过 tenant/instance 授权；所有端看到相同身份、事实和操作回执 |

每批先提供测试保护，再增加自动化。任务绑定工具版本，更新从新任务或明确检查点启用。
清场、外发和自动修复没有因这个只读入口而获得授权；硬门禁和合并权限不改变。
