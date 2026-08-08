# ai-batch-runner 实际使用反馈（2026-08-08）

> 场景：6 项目连接分析流水线（connection-analysis-pipeline.yaml）+ 三原则
> 落地（认证统一/审计全接入/通知统一）——跨 7 仓库、约 4 小时连续使用。
> 本文记录工具集在实际业务中的**正反馈**与**痛点**，供工具自身迭代。

## 一、正反馈（实际收益）

1. **连接分析流水线价值高**：evidence→kickoff→7 分项目→meta 对抗→gate 的
   结构产出 10 份文档（连接矩阵/每项目接口清单/29 项改动），gate 终裁 PASS
   带硬条件——**对抗审查确实抓到了 kickoff 的 5 处证据精度错误**（4 采信
   1 驳回，如 aero-im 自签发 JWT 修正）。
2. **check 前置门禁有效**：流水线 YAML 的结构校验（阶段名/上游引用/任务类型）
   在 dry-run 前拦截错误。
3. **decision-log 追加式**：全部阶段 PASS/FAIL 留痕（含协议专家失败 2 次），
   复盘有据。
4. **失败签名拒绝**：provider 400 的 agent 输出不落盘（18s 快速失败被拒），
   不会污染产物。
5. **--reuse 断点续跑**：进程意外退出后重跑复用全部已验证产出（evidence/
   kickoff/7 项目 0 模型调用），只补 review+gate。
6. **meta_max_failed_roles 配置**：单个角色持续失败时允许其余角色覆盖审查，
   流水线继续到 gate（缺失审查由 gate 兜底）——文档化后是强韧性开关。

## 二、痛点与改进建议（按影响排序）

### P1：from_dir 每文件一任务（本次最大浪费）
- **现象**：`from_dir: docs/snaplink-platform` 产生 17 个任务（每个文档一个
  LLM 调用），实际只想"把目录内容合并作为单一输入"。`aggregate: true`
  对 from_dir **不生效**（只作用于 from_outputs）。
- **后果**：证据归一阶段 17 次模型调用（30-50 分钟），大多是无意义的单文档
  总结；首次运行误启动后被迫 kill 重构为子目录（5 文件）。
- **建议**：`from_dir` 支持 `aggregate: true` → 单任务合并（文件分界符分隔，
  沿用 evidence.max_bytes 有界读取）；或支持 `from_dir + tasks` 模板覆盖
  （指定一个任务读取全部）。

### P2：GATE PASSED 与 "Failed stages" 信息矛盾
- **现象**：gate PASS 的同时日志打印 `ERROR Failed stages: (task failures)`
  + `Max rounds (1) reached`——失败来自 meta 角色的（已允许）失败，但日志
  措辞像流水线失败。
- **后果**：容易误判流水线结果；nohup 场景下人工核对困难。
- **建议**：failed stages 统计区分"允许的失败"（meta_max_failed_roles 内/
  重试后仍失败的已豁免角色）与"阻断失败"；gate PASS 时只提示豁免项。

### P3：进程意外退出无原因记录
- **现象**：review 阶段后流水线进程消失（无 traceback、无退出码），靠
  `ps` 才发现；恢复只能靠 --reuse 重跑猜。
- **建议**：主循环 catch 顶层异常并写退出原因（signal/panic/超时）到
  log + `.pi-batch/` 状态文件；`--resume` 时可显示上次退出原因。

### P4：provider 模型不可用的快速失败与重试
- **现象**：Console provider 间歇 `400 Model is unavailable`，agent 18s
  快速失败；`--retries 2` 的指数退避对 4xx 仍是普通退避（无 jitter 区分）。
- **建议**：对 provider 级错误（model unavailable/quota）采用更长固定等待
  （如 60s）+ 连续失败熔断提示（"建议 --round-delay 后重试"）。

### P5：dry-run 与实际 Loaded 数不一致
- **现象**：dry-run 显示阶段模板数（1/7/0/1）但不显示 from_dir 的实际任务
  数；实际运行 `Loaded 17 tasks` 才暴露。
- **建议**：dry-run 对 from_dir 阶段打印 `Will read N files from: ...`（文件
  数），让用户在启动前知道成本。

### P6：并行会话锁的误报体验
- **现象**：并行 campaign 与流水线同库跑时，日志出现
  `Another pi-batch instance is running (pid=自己)`——锁判定含自己 argv
  的瞬时窗口（或 argv 归一问题），造成"自己锁自己"的困惑。
- **建议**：锁检测排除自身 pid；冲突时打印对方 argv 的摘要（当前已打印）
  并区分 dry-run（dry-run 不应持锁）。

### P7：meta 角色失败的诊断信息
- **现象**：角色失败（18s）只记 `agent exited 1`，stderr 在日志尾部才出现
  （provider 400 详情）；排查要翻日志尾部。
- **建议**：失败摘要行内联 stderr 首行（如 `FAIL [code=1] model unavailable`）。

## 三、本次使用沉淀（流程层面）

1. **evidence 目录瘦身**：证据归一用"子目录 + 关键文档"而非全量目录——
   文档应在流水线 YAML 注释里提示。
2. **HMAC/bash 展开坑**（跨仓库操作）：bash 变量传入 heredoc 会污染密钥——
   用 python 直读文件；工具集可加"敏感值不经过命令行"的检查提示。
3. **schema/source 注册排障链**已在平台文档固化（三原则落地记录）——
   工具集层面的"跨项目接线"模式值得沉淀为模板（connection-analysis
   pipeline 已成为资产）。

## 四、后续

- P1（from_dir aggregate）优先实现（本次直接痛点）。
- P2/P5 一行级改进（信息准确性）。
- P3 退出原因记录（恢复体验）。
