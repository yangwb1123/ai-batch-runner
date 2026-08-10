# ForgeOS 实战反馈 —— ai-batch-runner 在大型双语言仓库上的真实战役

> 场景:在 [ForgeOS](https://github.com/forge-os)(Go 核心 + Rust runtime + Node/Python
> harness 的多语言仓库,~1400 源文件、每轮验收 `forge accept` 需 ~10 分钟)上连续跑
> campaign 两轮(forge-core、forge-runtime 模块)。本文件记录上游由此获得的修复
> (均已落地)与操作经验。

## 已落地的上游修复

### 1. 证据路径缺少模块前缀导致方向被误拒(最高价值)

**症状**:分析 agent 输出真实、高价值方向,但 campaign 报
`no evidence path exists in the repository (evidence: internal/statefs/statefs.go:276-313)`,
方向被整轮丢弃,浪费一轮完整分析(约 10-15 分钟,含一次超时重试)。

**根因**:分析模型引用证据时省略了模块前缀 —— 文件实际在
`forge-core/internal/statefs/statefs.go`,模型写成了 `internal/statefs/statefs.go`。
多模块仓库(尤其 roots 是目录而非单层时)几乎必然发生;旧逻辑只按原样探测。

**修复**(`pbatch/campaign_analysis.py::evidence_exists`):每个候选路径先按原样探测,
不存在时再用 `{direction.module}/{candidate}` 探测(先原样后加前缀,保证带完整前缀的
模型输出不受影响;`_inside` 继续拒绝任何逃逸 root 的路径)。测试:
`tests/test_campaign.py::test_evidence_missing_module_prefix_is_probed_via_direction_module`。

### 2. implement 阶段失败/被杀后没有"半成品清单"

**症状**:implement 阶段超时(TIMEOUT),campaign 进程随后被外部 SIGKILL —— 工作树留下
约 28 个已修改文件,但**没有任何清单**说明:哪些文件被动过、改动多大、能否编译。
接手者(人或另一个 agent)只能自己 `git diff` 从头推导,且无法区分"agent 改到一半的
半成品"与"已完成但没提交的成品"。

**修复**(`pbatch/campaign_pipeline.py::_write_wreckage_report`):方向管线失败时,把
`git status --porcelain` 与 `git diff --stat` 快照写入 run 目录的 `wreckage.md`
(带变更状态分类摘要)。下次接手者/复用逻辑可以直接从报告开始,而不是重新推导。

### 3. SIGKILL 后日志无 liveness 痕迹

**症状**:进程被外部杀死时,日志停在 stage 头一行,无法判断"卡住了"还是"刚启动"。
分析超时(TIMEOUT)至少还有 `[ERROR] TIMEOUT` 行;SIGKILL 则什么都没有。

**修复**(`pbatch/campaign_exec.py::_start_heartbeat`):daemon 线程每 5 分钟写一行
`CAMPAIGN HEARTBEAT: alive after HH:MM:SS`。SIGKILL 下最后一行即存活证明;
日志静默间隙直接告诉操作者进程死于哪个阶段。

## 操作经验(不需要改代码)

### 分析阶段
- **大模块分析会超时**:forge-core(36k 行 Go)分析 ~113s;forge-runtime(Rust
  workspace)首次分析在默认 900s 超时,重试才成功。大仓库请用
  `--timeout 1800`(该 flag 已透传给分析任务)。
- **分析输出被叙述污染**:即使 prompt 要求 "Return ONLY a JSON array",模型仍可能加
  前言/围栏。用 config 覆盖 `analysis.prompt`,把要求写成
  "the first character must be '[' and the last ']'. No prose before or after."
  后,问题消失(两轮分析一次通过)。`_extract_json_array` 已有容错,但防污染比容错便宜。

### 实现阶段
- **全量验收门禁跑不过 stage 超时**:ForgeOS 的 `forge accept`(聚合 arch/gate/测试)
  约 10 分钟;配合模型的编译+测试循环,1800s 的 implement 超时必然不够(两轮都超时)。
  按仓库门禁成本调大 pipeline 模板中 implement 的 `timeout`(ForgeOS 用 3600s)。
- **超时不是白费**:即使 implement 超时,agent 留下的改动常常完整可用(两轮都是:
  第一轮 26 文件全绿,第二轮 28 文件只有少量编译错误)。流程化接手 = 读 wreckage.md
  → 编译 → 修 import/可见性小错 → 跑验收。两轮都以此方式救回了全部实现。

### 通用
- **campaign 状态产物值得提交进仓库**:`state/analyses/*.json` +
  `state/runs/<direction>/artifacts/`(requirements/design/对抗评审/design gate)
  是完整的评审证据链。ForgeOS 将 `ai-campaign/state/` 提交入库(与 docs/reviews/
  同惯例),评审与回归都能引用具体文件。
- **design gate + 多角色对抗评审确实拦截问题**:两轮共 11 个角色评审文档,设计门两次
  PASS;评审发现的方向(无界子进程、缺 CLI 裁决通道)都是真实缺陷,证据驱动分析
  (要求模型给真实文件路径)没有产生空想方向。

## 环境备注

- 上游仓库与目标仓库在同一台机器上并行开发时,双方工作树都可能被其他 agent 改动;
  campaign 的 lock 机制保护的是 campaign 自身,不保护目标仓库的工作树。在目标仓库
  上跑 campaign 前先确认工作树干净(`parallel_ready` 已在做)。
- implement 直接改目标仓库主分支。若目标仓库有多个 agent 并发,建议 campaign 在
  独立 worktree 上跑(上游已有 worktree_root 配置,ForgeOS 场景未启用)。
