# T10：Review Finding 自动修复闭环

状态：已交付。该阶段复用 T9 的审查结果与既有 gate-fix 纪律，形成 headless 串行修复流程。

## 目标

将结构化 findings 按文件聚合，逐文件执行修复和可选验证，并输出稳定的 Markdown/JSON 结果。

## 已交付

- `pbatch/review.py`：暴露可复用的审查运行与 finding 提取入口，CLI 行为保持不变。
- `pbatch/review_fix.py`：实现 `pi-batch review-fix`、按文件聚合、串行修复、验证重试和有界报告。
- `pbatch/cli.py`：注册 `review-fix` 子命令。
- `tests/test_review_fix.py`：覆盖聚合顺序、修复状态、验证重试、clean/review failure、报告结构和路径越界。

## 正向流程

```text
只读 review
  → 归一化 findings
  → 按 file 聚合
  → 单写者串行修复
  → 可选 validator（失败后最多重试一次）
  → 汇总报告
```

每个修复 prompt 明确要求核对实际代码、限制改动范围、不提交，并携带该文件的结构化 findings。工具记录修复前后的工作区路径集合，但不把模型自述当作改动真值。

## 结果契约

- 无 finding：`clean`，退出码 `0`。
- review 超时、非零退出或破坏只读边界：`review_failed`，退出码 `2`。
- 修复完成后，仍有 critical/high finding 对应文件未修复：退出码 `1`；否则退出码 `0`。
- `--validate` 中的 `{files}` 与 `{cwd}` 由工具受控展开；报告路径必须位于工作区内。

## 验证入口

```bash
python -m pytest tests/test_review_fix.py tests/test_review.py tests/test_pi_batch.py tests/test_entrypoints.py -q
python quality.py --strict .
python pi-batch.py eval
```

## 当前边界

流程采用单 worktree、单写者串行策略；它不会自动 commit，也不替代仓库最终工程门禁。
