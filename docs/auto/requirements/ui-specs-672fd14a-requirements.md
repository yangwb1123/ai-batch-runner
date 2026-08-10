# Requirements Specification — uicheck/uigeometry/uiquality 扫描目录改为按项目可配置（替代硬编码 {cwd}/src）

| Field | Value |
|---|---|
| Module | `ui-specs` |
| Direction id | `672fd14a` |
| Source analysis | `docs/auto/analyses/ui-specs-672fd14a.json` (direction #2) |
| Status | Proposal (evidence-verified) |
| Scope | This direction only — no expansion |

---

## 1. Summary

`pi-batch.yaml` 把三个项目级 UI 门禁验证器注册为**字面量** `--dir {cwd}/src`：

- `uicheck`（:163）`check-ui-spec.py --dir {cwd}/src --all --json -o {cwd}/docs/ui/refactor/report.json`
- `uigeometry`（:165）`check-ui-geometry.py --dir {cwd}/src --all --json -o {cwd}/docs/ui/refactor/geometry.json`
- `uiquality`（:171）`check-frontend-quality.py --dir {cwd}/src --strict --json -o {cwd}/docs/ui/refactor/quality.json`

前端代码不在 `src/`（如 `lib/`、`app/`、`frontend/`）的项目会被扫描为**零文件**，
而三个脚本对零文件**按设计 exit 0**（`check-ui-spec.py` docstring 明言 *"No
frontend files found -> exit 0"*），于是门禁在什么都没检查的情况下报成功——
**静默假绿**。本仓库自身即实证：仓库根没有 `src/` 目录，但
`docs/ui/refactor/report.json` 存在且内容为 `files_scanned: 0, total: 0`
（门禁对"无"报告成功）。`examples/ui-refactor-pipeline.yaml:14` 的注释
*"把 pi-batch.yaml 的 uicheck 验证器 --dir 改成项目前端根目录"* 说明当前
适配只能靠人工手改注册表，且漏改时无任何信号。

本方向要求：(1) `pi-batch.yaml` 提供按项目配置扫描目录的机制（`ui_scan_dir`
顶层键 + `{ui_scan_dir}` 占位符，运行时展开）；(2) `uicheck` 在配置了
`--dir` 却扫到零个前端文件时向 stderr 输出显式警告（不再只是裸 OK）；
(3) 非 `src/` 布局（如 `lib/`）的项目能真实扫描到文件；(4) `make ci` 与
现有 `tests/test_ui_check.py` 全部保持通过。

---

## 2. Evidence verification

方向引用的每个证据点均对照当前仓库重新核验（行号以当前文件为准；全部
属实，其中 `backendquality` 一项为问题陈述中的过时提及，见下）。

| 引用证据 | 核验结果 |
|---|---|
| `pi-batch.yaml:163` | ✅ **精确**。`uicheck: "python {cwd}/scripts/check-ui-spec.py --dir {cwd}/src --all --json -o {cwd}/docs/ui/refactor/report.json"` —— 字面量 `{cwd}/src` 硬编码。 |
| `pi-batch.yaml:165` | ✅ **精确**。`uigeometry: "python {cwd}/scripts/check-ui-geometry.py --dir {cwd}/src --all --json -o {cwd}/docs/ui/refactor/geometry.json"` —— 同样硬编码 `{cwd}/src`。 |
| `pi-batch.yaml:171` | ✅ **精确**。`uiquality: "python {cwd}/scripts/check-frontend-quality.py --dir {cwd}/src --strict --json -o {cwd}/docs/ui/refactor/quality.json"` —— 同样硬编码 `{cwd}/src`。 |
| `scripts/check-ui-spec.py:24` | ✅ **精确**。:24 注释 *"Project scan (--dir) walks the directory recursively, skipping build/vendor"*，:26 *"files found -> exit 0, so the gate is safe for repos without frontend code"* —— 零文件即通过是文档化的设计。 |
| `scripts/check-ui-spec.py:26` | ✅ **精确**（同上，同段 docstring 的下一行）。 |
| `docs/ui/refactor/report.json` | ✅ **精确**。内容为 `{"mode":"all","directory":"/home/u1/ai-batch-runner/src","files_scanned":0,"violations":[],"total":0}`；且仓库根 `ls -d src` → `No such file or directory` —— 门禁对不存在的目录报告成功。 |

### 额外核验（超出引用清单但直接支撑方向事实）

| 事实 | 证据 |
|---|---|
| 三个脚本对零文件全部 exit 0（假绿全家桶） | `check-ui-spec.py` `main()`：零文件 → `report.files_scanned == 0`、violations 空 → return 0；`check-ui-geometry.py:202` `return 0 if not all_problems else 1`（:190 报告 `"files": len(targets)`）；`check-frontend-quality.py:295-297` 打印 `UI-QUALITY: OK (0 file(s) scanned)` 后 `return 0`。 |
| 三份历史报告全部是零信号 | `docs/ui/refactor/geometry.json` = `{"files": 0, "violations": {}}`；`docs/ui/refactor/quality.json` = `files_scanned: 0, total: 0`（与 report.json 同一模式）。 |
| 唯一占位符展开点在 runner | `pbatch/runner.py:487`（fresh-save 验证）与 `:698`（reuse 复验）均执行 `spec.cmd.replace("{output}",...).replace("{cwd}",...).replace("{tool_root}",...)` —— 目前**只有这三个占位符**，无任何项目变量机制（全仓 grep 无 `ui_scan_dir`/`{var}`/`scan_dir` 命中）。 |
| 配置读取模式 | `pbatch/config.py:26-47` `_find_batch_config()` 定位并解析 pi-batch.yaml（入口脚本旁 → 包目录 → 工作目录）；:55 `_BATCH_CFG = _load_batch_config()` 在 import 时载入；:419-430 `_load_validators()` 从同一文件读 `validators:` 段。新增顶层键应沿用此模式。 |
| `backendquality` 不在影响面 | ⚠️ **问题陈述过时**：`backendquality`（pi-batch.yaml:174-179）已经是 `--dir {cwd}`（非 `{cwd}/src`），且自带 manifest 感知 fail-closed（"no backend manifest detected; gate skipped" 显式信号、0 文件 exit 1）。**不受本方向影响，列为 non-goal**。 |
| 现有测试锚点（必须保持通过） | `tests/test_ui_check.py:57-61` `test_dir_scan_no_frontend_files_passes`：零文件目录 → **returncode == 0** 且 `"0 file(s) scanned" in result.stdout`；:138-149 `test_json_clean_path_stdout_pure`：`--json` 时 **stdout 必须是纯 JSON**（`"UI-SPEC" not in result.stdout`）；:102-112 `test_ui_refactor_pipeline_schema_and_gate`：`_resolve_validators("uicheck")[0]` 必须含 `"check-ui-spec.py --dir"` 与 `"--all"` 子串。 |
| 现有文档已承认人工适配步骤 | `examples/ui-refactor-pipeline.yaml:14` 注释 *"把 pi-batch.yaml 的 uicheck 验证器 --dir 改成项目前端根目录"* —— 本方向把该人工步骤机制化。 |
| CI 入口 | `Makefile:47` `ci: check self-test test quality org`，其中 `test` 为 `python -m pytest tests/`。 |

### 实测复现（当前仓库，全部 live）

```console
$ ls -d src
ls: cannot access 'src': No such file or directory

$ python scripts/check-ui-spec.py --dir src --all --json 2>&1
UI-SPEC [all]: OK (0 file(s) scanned)        # ← 裸 OK，stderr 无任何警告
{ "mode": "all", "directory": "src", "files_scanned": 0, "violations": [], "total": 0 }
exit=0

$ python scripts/check-ui-geometry.py --dir src --all --json; echo exit=$?
{ "files": 0, "violations": {} }             # ← 零信号
exit=0

$ python scripts/check-frontend-quality.py --dir src --strict --json; echo exit=$?
UI-QUALITY: OK (0 file(s) scanned)           # ← 裸 OK
exit=0
```

---

## 3. Root cause

1. **注册表写死字面量**：三个 validator 的 `--dir` 值是 `pi-batch.yaml` 里的
   字符串常量，运行时只有 `{output}/{cwd}/{tool_root}` 三个占位符可展开
   （`pbatch/runner.py:487,:698`），没有项目级变量可引用 → 布局非 `src/`
   的项目只能手改 yaml 或静默假绿。
2. **零文件即通过且无警告**：`scan_directory`（`check-ui-spec.py:122-133`）
   对不存在/无前端文件的目录返回空列表，`main()` 据此产出
   `files_scanned: 0` + exit 0，并只打印 `OK (0 file(s) scanned)` ——
   "门禁在跑"的错觉没有任何反证信号。

修复 = 机制化（占位符 + 顶层键）+ 可见化（零文件警告），两个改动各自独立
可测，互不依赖。

---

## 4. Requirements

### R1 — pi-batch.yaml 支持按项目配置扫描目录（AC1）

`pi-batch.yaml` 新增**可选顶层键** `ui_scan_dir:`；`uicheck`/`uigeometry`/
`uiquality` 三个验证器命令中的扫描目录改用 `{ui_scan_dir}` 占位符，
由 runner 在命令执行时展开（与 `{output}/{cwd}/{tool_root}` 同一展开点，
`pbatch/runner.py:487` 与 `:698`，建议提取共享 helper 防止两处漂移）。

- 语义：`ui_scan_dir` 未配置 → 展开为 `{cwd}/src`（**保持现状，向后兼容**，
  现有项目零迁移）；配置为相对路径 → 相对任务工作目录（`{cwd}`）解析；
  配置为绝对路径 → 原样使用。
- 三个条目全部切换后，`pi-batch.yaml` 中不再残留字面量 `{cwd}/src`；
  原注释 *"Adapt --dir to the project's frontend root"* 同步改为指向
  `ui_scan_dir`。

**可测检查：**

- T1.1 `_resolve_validators("uicheck")[0]`（及 `uigeometry`/`uiquality`）
  含 `"--dir {ui_scan_dir}"` 且不含 `"{cwd}/src"`；同时现有断言
  `"check-ui-spec.py --dir"`、`"--all"` 子串（`tests/test_ui_check.py:110`）
  继续成立。
- T1.2 对共享展开 helper 的单元测试（monkeypatch 配置值）：
  - `ui_scan_dir = "lib"` → 展开为 `--dir <cwd>/lib`
  - `ui_scan_dir` 未设置 → 展开为 `--dir <cwd>/src`（默认不变）
  - `ui_scan_dir = "/abs/frontend"` → 展开为 `--dir /abs/frontend`
- T1.3 `tests/test_ui_check.py` 新增回归：`_resolve_validators` + 展开
  结果断言（覆盖 T1.1/T1.2 的默认与配置两个分支）。

### R2 — uicheck 零文件扫描向 stderr 告警（AC2）

`scripts/check-ui-spec.py` 在 `--dir` 已配置（项目扫描模式）且目录扫描产出
**零个**前端文件时，向 **stderr** 输出显式警告（含 `0 file(s) scanned`、
"warning" 字样、被扫描目录路径），**exit code 保持 0**；`--json` 模式下
stdout 仍只含 JSON（警告走 stderr，`test_json_clean_path_stdout_pure` 契约
不变）。单文件模式（无 `--dir`）不触发该警告。

**可测检查：**

- T2.1 新回归测试：`--dir <空目录> --all` → returncode == 0，stderr 含
  `warning` 与 `0 file(s) scanned`，stdout 仍含 `OK (0 file(s) scanned)`
  （现有 `test_dir_scan_no_frontend_files_passes` 的两条断言原样成立）。
- T2.2 `--dir <空目录> --all --json` → returncode == 0，`json.loads(stdout)`
  可解析且 `files_scanned == 0`，警告仅出现在 stderr。
- T2.3 单文件模式：`--mode spacing <合规文件>` → 不输出目录警告。
- T2.4 有前端文件的目录 → 不输出警告（`files_scanned > 0` 时静默如常）。

### R3 — 非 src/ 布局项目真实扫描（AC3）

前端代码在 `lib/`（或任何非 `src/` 目录）的项目配置 `ui_scan_dir: lib`
后，`uicheck` 真实扫描 `lib/` 下文件（`files_scanned > 0`），违规照常
exit 1、报告照常产出。

**可测检查：**

- T3.1 脚本级：tmp 项目 `lib/components/Button.tsx` 含 `margin: 11` →
  `--dir <tmp>/lib --all --json` → exit 1、`files_scanned == 1`、violations
  含该行。
- T3.2 合规树：`lib/` 下仅 token 合规文件 → exit 0 且 `files_scanned > 0`
  （非 0，验证不是"扫了等于没扫"）。
- T3.3 接线级：配置 `ui_scan_dir: lib` 时 T1.2 展开结果为
  `--dir <cwd>/lib`，对该 tmp 项目执行展开后的命令 → `files_scanned > 0`。

### R4 — 回归门禁（AC4）

**可测检查：**

- T4.1 `python -m pytest tests/test_ui_check.py` 全绿（现有 12 个 + 新增
  回归；重点：`test_dir_scan_no_frontend_files_passes`、
  `test_json_clean_path_stdout_pure`、`test_ui_refactor_pipeline_schema_and_gate`）。
- T4.2 `make ci` 全绿（check + self-test + pytest tests/ + quality + org）。

---

## 5. Test plan

新增测试全部落在 `tests/test_ui_check.py`（项目扫描检查器家族既有回归
宿主），零新依赖：

| 测试 | 覆盖 | 对应检查 |
|---|---|---|
| `test_validator_cmd_uses_ui_scan_dir_placeholder` | `_resolve_validators` 三个条目含 `--dir {ui_scan_dir}`、无 `{cwd}/src` | T1.1 |
| `test_ui_scan_dir_expansion_default_and_configured` | 展开 helper：未配置→`<cwd>/src`；`lib`→`<cwd>/lib`；绝对路径原样 | T1.2, T3.3 |
| `test_dir_scan_zero_files_warns_on_stderr` | 空目录：exit 0、stderr 警告、stdout 摘要不变 | T2.1 |
| `test_json_zero_files_warning_stderr_only` | `--json`：stdout 纯 JSON、警告走 stderr | T2.2 |
| `test_no_warning_single_file_mode` | 无 `--dir` 不告警 | T2.3 |
| `test_dir_scan_lib_layout_finds_files` | `lib/` 布局：违规 exit 1 且 `files_scanned == 1`；合规 exit 0 且 `files_scanned > 0` | T3.1, T3.2 |

回归锚点（不允许破坏）：`tests/test_ui_check.py:57-61`（零文件 exit 0 +
stdout 摘要）、`:138-149`（stdout 纯 JSON）、`:102-112`（validator 命令
含 `--dir`/`--all`）。

## 6. Non-goals（刻意不扩展）

- **不改默认扫描根**：未配置 `ui_scan_dir` 时保持 `{cwd}/src`（现状），
  不改为 `{cwd}` 或其他目录——那会改变既有 src 布局项目的扫描范围。
- **不给 uigeometry/uiquality 加 stderr 警告**：AC2 只覆盖 uicheck；两个
  脚本的 JSON 报告已暴露 `files_scanned: 0`，警告扩展留作后续方向。
- **不动 `backendquality`**：已核验其使用 `--dir {cwd}` + manifest 感知
  fail-closed，问题陈述中的提及为过时信息。
- **不改扫描语义**：`DEFAULT_EXTS`/`DEFAULT_EXCLUDES`、`--json` stdout
  契约、退出码语义（零文件仍 exit 0）均不变。
- **不新增 CLI 参数**：配置面只有 pi-batch.yaml 顶层键 `ui_scan_dir`。

## 7. 设计决策与备选

| 决策 | 选择 | 理由 |
|---|---|---|
| 机制形态 | 顶层键 `ui_scan_dir` + 占位符 `{ui_scan_dir}`（AC1 的首选示例） | 与现有顶层键（`agent:`/`limits:`/`validators:`）及 `{tool_root}` 占位符模式一致；实现 = config.py 读取 + runner 两处展开点追加一个 replace，改动面最小 |
| 备选（否决） | validator 映射形式 `uicheck: {cmd: ..., vars: {scan_dir: lib}}` | 更通用但引入新的嵌套 schema 与校验，超出本方向所需 |
| 备选（否决） | 各项目 fork pi-batch.yaml 手改字面量目录 | 正是现状（`examples/ui-refactor-pipeline.yaml:14` 注释描述的人工步骤），无信号、易漂移 |
| 默认值 | 未配置 → `{cwd}/src` | 向后兼容，现有项目零迁移；"非 src 布局没信号"由 R2 警告暴露，不再静默 |
| 警告通道 | stderr，exit 0 不变 | 不破坏 `test_dir_scan_no_frontend_files_passes`（exit 0 + stdout 摘要）与 `--json` stdout 纯 JSON 契约；stderr 恰是 runner 喂给重试 prompt 的通道 |

## 8. 落地触点（实现指引）

- `pi-batch.yaml`：`validators:` 段三个条目 `--dir {cwd}/src` → `--dir {ui_scan_dir}`；新增顶层 `ui_scan_dir:`（示例值注释）；更新 "Adapt --dir" 注释。
- `pbatch/config.py`：`_BATCH_CFG` 读取 `ui_scan_dir`（未配置为 None）。
- `pbatch/runner.py`：:487 与 :698 两处展开链追加 `{ui_scan_dir}`（建议提取共享 helper `_expand_cmd`），相对值以任务 workdir 解析、绝对路径原样、None → `{cwd}/src`。
- `scripts/check-ui-spec.py`：`main()` 在 `directory is not None` 且扫描零文件时向 stderr 打印警告（含目录路径与 `0 file(s) scanned`），exit 0 不变。
- `tests/test_ui_check.py`：按 §5 新增 6 个回归测试。
- `examples/ui-refactor-pipeline.yaml:14` 注释同步为 `ui_scan_dir` 指引（文档对齐，1 行）。
