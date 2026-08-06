已完成新证据归一。

**产出**：`docs/snaplink-platform/erp-inputs/04-gate-verification.out.md`

流水线现为 7 阶段（evidence → kickoff → review → gate → **revision → review2 → gate2**），本任务为该轮 evidence 阶段 [1/1]。01–03 已 REUSE，04 为新增归一：

- B1–B5 人工复核结论原样保留（契约不兼容 / actor 丢失 / 测试门禁红 / 生产零审计事实 / 无 git 仓库），全部 Verified
- 关键修正依据落盘：**kickoff 的 P0"设置 env 即激活、零代码"论断被推翻**，P0 改为「契约对拍（适配器）+ 先 schema 注册再指 URL」
- 5 条修订要求逐项保留（kickoff 回写 P0 修正与证据降级、M0→M1 门禁转绿硬门槛、8 项 owner 决策、git/CI 纳入 M4 前置、采纳项保留清单）

下游 revision 阶段将以本文件为事实依据回写 kickoff 产出。
