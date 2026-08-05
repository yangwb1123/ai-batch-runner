# CHECKS_REGISTRY — ai-batch-runner 工程门禁注册表

本仓库的工程质量门禁体系仿照 `snaplink` 的质量管理（`cli.py` + `checks/` +
`engineering.yaml` + `make ci` + git hooks + CI），针对 Python 项目做了适配。
核心原则与 snaplink 一致：

1. **零依赖可移植**：门禁只用标准库（ast/unittest），复制 `cli.py` +
   `checks/` + `engineering.yaml` 到任意 Python 项目即可复用，只改 YAML 配置。
2. **门禁约束自研代码**：与上游 `snaplink/ai-dev` 同步的引擎文件
   （`pi-batch.py`、`ai/run-review.py`）通过 `engineering.yaml` 的
   `ignore_patterns` 豁免——质量规则只约束我们自主编写的代码。
3. **门禁自身有单测**（`checks/test_*.py`，`make self-test`）：检查器坏了
   必须能被发现。

## 门禁清单

| 门禁 | 命令 | 阈值 | 说明 |
|---|---|---|---|
| G1 filesize | `make check-filesize` | 500 行/文件 | 源文件行数预算；豁免清单（`exemptions`）只减不增 |
| G2 complexity | `make check-complexity` | 圈复杂度 15 / 认知复杂度 20（每函数） | ast 实现，替代 gocyclo/gocognit |
| G3 root policy | `make check-root` | 根目录 ≤15 文件 | 根目录只允许配置/入口/文档；禁止 `*_test.py` 散落根目录 |
| G4 style | `make check-style` | 行宽 100（警告） | 行尾空白、tab 缩进、缺末尾换行、非 utf-8 为硬失败 |
| G5 quality | `make quality` | 函数 50 行 / 圈复杂度 15 / 文件 1000 行 / 重复函数体 | 参考版自带扫描器 `quality.py`（纯 stdlib），经 `pi-batch.yaml` 注册为 `validators.pyquality` |
| H0 self-test | `make self-test` | — | 门禁自身单测（34 例，unittest 零依赖） |
| T0 regression | `make test` | — | 引擎、规则系统/评估器/检查器与 Campaign 回归套件（387 例 + 25 eval 用例，pytest，需 dev extras） |
| T1 coverage | `make coverage` | — | checks/ + cli.py 覆盖率报告（需 pytest-cov） |

## 使用

```bash
make check          # G1-G4 零依赖门禁
make self-test      # H0 门禁自身单测
make quality        # G5 组织质量扫描（quality.py，上游同步）
make test           # T0 回归套件（pip install -e ".[dev]"）
make ci             # 全部（CI 与 pre-push 执行此集）
python cli.py check-filesize   # 单独跑某门禁
```

## 配置（engineering.yaml）

各门禁阈值与豁免均在 `engineering.yaml` 中声明：

- `filesize.max_lines` / `ignore_patterns` / `exemptions`
- `complexity.max_cyclomatic` / `max_cognitive` / `ignore_patterns`
- `root_policy.max_files` / `allowed_files` / `banned_patterns`
- `style.max_line_length` / `ignore_patterns`

`ignore_patterns` 为子串匹配（snaplink 语义）。新增业务代码必须通过全部门禁；
需要放宽阈值时先改 YAML 再提交，并在 commit message 中说明理由。

## Git hooks

```bash
git config core.hooksPath .githooks
```

- `pre-commit`：对暂存的 `.py` 文件跑 filesize + style 门禁（豁免规则同上）
- `pre-push`：跑 `cli.py harness`（= `make check` + `make self-test`）

## 上游同步豁免

`pbatch/`（引擎包）、`pi-batch.py`（入口 shim）、`ai/run-review.py`、`quality.py`
均为与 `snaplink/ai-dev` 同步的上游文件，经 `engineering.yaml` 的
`ignore_patterns` 豁免（`pbatch/pipeline.py` 853 行在参考版 1000 行预算内；
参考版自带的行尾空白与复杂度超过本仓库更严预算的少数函数一并豁免）。
质量规则实际约束自研代码：`checks/`、`cli.py`、`Makefile` 及新增业务代码。

## CI

`.github/workflows/ci.yml`：Python 3.11 + `pip install -e ".[dev]"`，依次执行
G1-G4、H0、T0、T1。推送到 `main` 或 PR 时触发（仿 snaplink engineering.yml）。
