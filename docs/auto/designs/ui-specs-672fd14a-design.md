# Design — uicheck/uigeometry/uiquality 扫描目录按项目可配置（`ui_scan_dir`）

| Field | Value |
|---|---|
| Module | `ui-specs` |
| Direction id | `672fd14a` |
| Upstream | `docs/auto/requirements/ui-specs-672fd14a-requirements.md`（R1–R4 / T1–T4） |
| Status | Design (evidence-verified, with 2 corrections beyond citations) |
| Scope | This direction only — no expansion |

---

## 0. 证据核验结论（untrusted evidence → verified）

全部引用证据逐条对照活代码树核验，**属实**；另有 2 处**超出引用清单**的
发现直接影响实现方案（§0.2），证据中的"唯一展开点在 runner"表述对这三个
验证器正确、但全局不完整。

### 0.1 引用证据核验

| 证据 | 核验 | 结果 |
|---|---|---|
| `pi-batch.yaml:163/165/171` | 三个验证器字面量 `--dir {cwd}/src`；:181 `backendquality` 已是 `--dir {cwd}` | ✅ 精确 |
| `check-ui-spec.py:24/26` | docstring "No frontend files found -> exit 0" | ✅ 精确 |
| `docs/ui/refactor/report.json` | `files_scanned: 0, total: 0`，`directory` 指向不存在的 `/home/u1/ai-batch-runner/src` | ✅ 精确 |
| `geometry.json` / `quality.json` | `{"files": 0}` / `files_scanned: 0, total: 0`（键名不同，均零信号） | ✅ 精确 |
| `runner.py:487/:698` | fresh-save 与 reuse 两条链仅展开 `{output}/{cwd}/{tool_root}` | ✅ 精确（对 scope=file 验证器） |
| `config.py:26-55,419-430` | `_find_batch_config`/`_BATCH_CFG`/`_load_validators` 模式 | ✅ 精确 |
| `test_ui_check.py:57-61,102-112,138-149` | 三个回归锚点逐行吻合；文件共 12 个测试 | ✅ 精确 |
| `backendquality` 过时提及 | 已 `--dir {cwd}` + manifest fail-closed，non-goal | ✅ 核验成立 |

### 0.2 超出引用清单的核验发现（直接影响方案）

- **F1 — 验证器命令占位符展开点共有 4 处，不是 2 处**：除 `runner.py:487/:698`
  （scope=file 验证器）外，`pbatch/repo_gates.py:31-32`（stage 末 repo-scope
  门禁）与 `pbatch/cli_batch.py:408`（批轮末 repo-scope 门禁）也在展开
  验证器命令。三个 UI 验证器今天都是 scope=file（`pi-batch.yaml` 纯字符串
  条目），只经过 runner 两处——证据表述对**当前三个条目**成立；但共享
  展开 helper 必须覆盖全部 4 处，否则未来某个 `scope: repo` 门禁若使用
  `{ui_scan_dir}`，字面量会漏进 shell（bash 不展开 `{ui_scan_dir}`，脚本
  收到 `--dir {cwd}/{ui_scan_dir}` 字面路径 → 目录不存在 → 假绿复活）。
  （`pbatch/pipeline.py:633` `_run_single_cmd` 是**阶段 post-command** 面而非
  验证器面——只展开 `{tool_root}`、不支持 `{cwd}`/`{output}`，本方向
  不扩展 stage commands，明确 non-goal。）
- **F2 — `cli_batch.py:408` 今天只展开 `{cwd}`（不展开 `{output}`），
  `repo_gates.py:31-32` 不展开 `{tool_root}`**：helper 需要保留这两处现状
  语义（`output=None` → 不触碰 `{output}`），只新增 `{ui_scan_dir}`；
  `{tool_root}` 缺口由 helper 顺带补齐，对现有 repo-scope 门禁（皆无
  `{tool_root}`）零行为变化。
- **F3 — 警告落点确认**：`check-ui-spec.py main()` 中 `--json` 模式摘要已在
  stderr（:250-251），非 json 模式 OK 摘要走 stdout（:261，回归锚点
  `"0 file(s) scanned" in result.stdout` 依赖此路径）；警告必须走 stderr
  （两种模式同一落点），不触碰 stdout 契约。

---

## 1. 方案总览

两个独立可测的改动：

1. **机制化（R1）**：`pi-batch.yaml` 新增可选顶层键 `ui_scan_dir:`；三个
   验证器命令 `--dir {cwd}/src` → `--dir {ui_scan_dir}`；`pbatch/config.py`
   新增共享展开 helper `expand_cmd()`，统一替换 4 处调用点的展开链。
2. **可见化（R2）**：`check-ui-spec.py` 在 `--dir` 项目扫描模式产出 0 个
   前端文件时向 stderr 输出显式警告，exit code 保持 0。

未配置 `ui_scan_dir` 时展开结果与今日**逐字节一致**（`--dir <cwd>/src`），
现有项目零迁移。

---

## 2. API 变更

### 2.1 配置面（pi-batch.yaml，新增 1 个可选顶层键）

```yaml
# 可选：项目前端扫描根（uicheck/uigeometry/uiquality 的 --dir 来源）。
# 未配置 → {cwd}/src（默认，向后兼容）；相对路径 → 相对任务工作目录解析；
# 绝对路径 → 原样使用。空字符串/纯空白 → 视为未配置。
ui_scan_dir: lib
```

- 读取点：`pbatch/config.py`（`_BATCH_CFG`，与 `agent:`/`limits:`/
  `validators:` 同源，import 时载入，函数访问时取值——测试可 monkeypatch）。
- 三个验证器条目（:163/:165/:171）命令改为：
  `--dir {ui_scan_dir}`（其余参数与输出路径不变）；:158-160 注释
  *"Adapt --dir to the project's frontend root"* 改为指向 `ui_scan_dir`。

### 2.2 内部 API（pbatch/config.py，新增 1 个函数 + 1 个 helper）

```python
def ui_scan_dir() -> Optional[str]:
    """项目配置的前端扫描根；未配置/空值 → None（展开为 {cwd}/src）。"""
    v = _BATCH_CFG.get("ui_scan_dir")
    return v.strip() if isinstance(v, str) and v.strip() else None

def expand_cmd(cmd: str, output, cwd: str, tool_root: str = TOOL_ROOT) -> str:
    """验证器命令占位符展开（唯一入口，全部 4 个调用点共用）。

    - {ui_scan_dir}: None → "{cwd}/src"（默认，向后兼容）；
                     相对值 → "{cwd}/<值>"（shlex.quote）；绝对路径 → 原样 quote。
    - {output}:      output 为 None 时保留字面量不动（cli_batch 现状语义）。
    - {cwd}/{tool_root}: 与现状一致。
    展开顺序：{ui_scan_dir} 先于 {cwd}（默认展开串自身含 {cwd} 占位符）。
    """
    usd = ui_scan_dir()
    if "{ui_scan_dir}" in cmd:
        if usd is None:
            repl = "{cwd}/src"
        elif os.path.isabs(usd):
            repl = shlex.quote(usd)
        else:
            repl = "{cwd}/" + shlex.quote(usd)
        cmd = cmd.replace("{ui_scan_dir}", repl)
    out = cmd
    if output is not None:
        out = out.replace("{output}", shlex.quote(str(output)))
    out = out.replace("{cwd}", shlex.quote(cwd)).replace("{tool_root}", shlex.quote(tool_root))
    if "{ui_scan_dir}" in out:  # 防御：任何遗留占位符都不该到达 shell
        log.error("expand_cmd: unresolved {ui_scan_dir} in %r", cmd)
    return out
```

### 2.3 4 个调用点替换

| 调用点 | 现状 | 改为 |
|---|---|---|
| `pbatch/runner.py:487`（fresh-save） | 内联链 `output/cwd/tool_root` | `expand_cmd(spec.cmd, tmp, task.workdir(), config.TOOL_ROOT)` |
| `pbatch/runner.py:698`（reuse 复验） | 同上 | `expand_cmd(spec.cmd, path, wd, config.TOOL_ROOT)` |
| `pbatch/repo_gates.py:31-32`（stage 末） | `{output}` 再 `{cwd}`，无 `{tool_root}` | `expand_cmd(spec.cmd, output_dir, wd, TOOL_ROOT)`（顺带补齐 tool_root） |
| `pbatch/cli_batch.py:408`（批轮末） | 仅 `{cwd}` | `expand_cmd(spec.cmd, None, wd, TOOL_ROOT)`（`output=None` 保留现状语义） |

### 2.4 脚本面（scripts/check-ui-spec.py，R2 警告）

`main()` 在 `paths = collect_targets(...)` 之后、violations 汇总之前插入：

```python
if directory is not None and not paths:
    print(f"UI-SPEC: warning: --dir {directory} scanned 0 file(s); "
          f"no frontend files found - gate passes vacuously "
          f"(check ui_scan_dir in pi-batch.yaml)", file=sys.stderr)
```

- 触发条件：`--dir` 已配置（项目扫描模式）且扫描产出零前端文件；单文件
  模式（`directory is None`）不触发（T2.3）。
- 通道：stderr（两种模式下同一位置，`--json` stdout 保持纯 JSON；非 json
  模式 stdout 的 `OK (0 file(s) scanned)` 摘要不动，T2.1 锚点成立）。
- exit code：不改变——violations 为空 → 现有路径 return 0。
- 文案含 T2.1 断言的 `warning` 与 `0 file(s) scanned` 子串；stderr 恰是
  runner 喂回重试 prompt 的通道，提示语兼具指导性（指向 `ui_scan_dir`）。

### 2.5 文档面

- `examples/ui-refactor-pipeline.yaml:14` 注释 *"把 pi-batch.yaml 的
  uicheck 验证器 --dir 改成项目前端根目录"* → 改为 `ui_scan_dir` 指引（1 行）。
- `docs/AUTOMATION_WORKFLOW_SUMMARY.md` / `RUNNING_247.md` 若提及
  `--dir {cwd}/src` 同步更新（核验时未见，仅在 pi-batch.yaml 注释中）。

---

## 3. 兼容性约束

| 约束 | 保障方式 |
|---|---|
| 未配置 → 行为逐字节不变 | 默认展开 `{cwd}/src` 与今日字面量等价；`{cwd}` 后续替换逻辑不变（含 shlex.quote 拼接语义 `'<cwd>'/src`，与现状一致） |
| `tests/test_ui_check.py:57-61`（零文件 exit 0 + stdout 摘要） | 警告走 stderr，stdout `OK (0 file(s) scanned)` 不变，exit 0 不变 |
| `:138-149`（`--json` stdout 纯 JSON） | 警告仅 stderr，stdout 仅 JSON payload |
| `:102-112`（`--dir`/`--all` 子串） | 新命令 `--dir {ui_scan_dir}` 保留两子串 |
| `--json` stdout 契约（家族基准） | 脚本仅新增 stderr 输出分支，无 stdout 改动 |
| 配置可移植性 | `ui_scan_dir` 可选；pi-batch.yaml 随项目复制，未配置项目零迁移 |
| scope 语义 | 三个验证器保持 scope=file，fresh-save/reuse 两路径展开一致 |
| 无关面 | `backendquality`、uigeometry/uiquality 脚本、`health`/`pi-batch check`（核验不消费验证器命令）、扫描语义（`DEFAULT_EXTS`/`DEFAULT_EXCLUDES`）均不动 |
| repo-scope 门禁现状语义 | `cli_batch.py` 的 `output=None` 保留"不展开 `{output}`"；`repo_gates.py` 顺带补齐 `{tool_root}`（现状无命令使用，零行为变化） |

---

## 4. 失败模式与对策

| 失败模式 | 表现 | 对策 |
|---|---|---|
| `ui_scan_dir` 拼错/目录不存在 | 0 文件 → uicheck 假绿 + **stderr 警告**（R2 把静默变可见） | 警告文案指向 `ui_scan_dir`；重试 prompt 携带警告 |
| `uigeometry`/`uiquality` 配置错误仍静默 | 零信号 JSON 报告 | 已接受的 non-goal（AC2 只覆盖 uicheck）；JSON 报告 `files: 0` 可人工/后续方向检出 |
| `ui_scan_dir` 含 shell 元字符/空格 | 路径解析错 | `shlex.quote` 展开（与 `{cwd}`/`{output}` 同策略） |
| `ui_scan_dir` 为空串/纯空白 | 误配置 | 视为未配置 → 默认 `{cwd}/src`（fail-safe 向后兼容） |
| 值含 `{`/占位符字样 | 双重展开或字面量残留 | 文档明示用纯路径；helper 展开后防御性检查并 log.error |
| 字面量 `{ui_scan_dir}` 泄漏到 shell | 假绿复活（F1 场景） | 全部 4 个展开点统一走 `expand_cmd`；helper 内防御检查 |
| 未来新增展开点忘用 helper | 同上 | 代码评审锚点：`grep -rn ".replace(\"{cwd}\"" pbatch/` 应为空 |
| 警告噪音进入重试循环 | agent 反复被"0 file(s) scanned"困扰 | 文案明示这是配置提示而非违规；exit 0 不触发重试 |

---

## 5. 迁移步骤

1. `pbatch/config.py`：新增 `ui_scan_dir()` 与 `expand_cmd()`（§2.2）。
2. `pbatch/runner.py:487/:698`、`pbatch/repo_gates.py:31-32`、
   `pbatch/cli_batch.py:408`：替换为 `expand_cmd`（§2.3，4 处）。
3. `pi-batch.yaml`：三个验证器命令改 `--dir {ui_scan_dir}`；注释区新增
   文档化示例键（§2.1）；更新 "Adapt --dir" 注释。
4. `scripts/check-ui-spec.py`：插入零文件 stderr 警告（§2.4）。
5. `tests/test_ui_check.py`：新增 6 个回归测试（§6）。
6. `examples/ui-refactor-pipeline.yaml:14` 注释同步。
7. 验证：`python -m pytest tests/test_ui_check.py` →
   `make ci` → `python quality.py --strict .`。
8. 下游项目：`src/` 布局零动作；`lib/` 等非 src 布局在 pi-batch.yaml 加
   一行 `ui_scan_dir: lib`（或对应路径）。

---

## 6. 可测验收映射（T ↔ R ↔ AC ↔ 测试）

| 验收检查 | 需求 | 可测检查 | 新增测试（tests/test_ui_check.py） | 关键断言 |
|---|---|---|---|---|
| AC1 目录可配置 | R1 | T1.1 三个条目命令含 `--dir {ui_scan_dir}`、不含 `{cwd}/src`；现有 `--dir`/`--all` 子串断言仍成立 | `test_validator_cmd_uses_ui_scan_dir_placeholder` | `_resolve_validators("uicheck"/"uigeometry"/"uiquality")[0]` 断言；monkeypatch `pbatch.config._BATCH_CFG` |
| AC1 | R1 | T1.2 展开 helper：`lib`→`<cwd>/lib`；未配置→`<cwd>/src`；`/abs/frontend`→原样 | `test_ui_scan_dir_expansion_default_and_configured` | `expand_cmd("--dir {ui_scan_dir}", None, "/proj")` 三分支 |
| AC1 | R1 | T1.3 默认与配置两分支接线 | 同上（两参数化用例） | — |
| AC2 零文件告警 | R2 | T2.1 空目录：exit 0、stderr 含 `warning`+`0 file(s) scanned`、stdout 摘要不变 | `test_dir_scan_zero_files_warns_on_stderr` | 复用 `_make_project({"README.md": ...})` 夹具 |
| AC2 | R2 | T2.2 `--json`：stdout 纯 JSON、`files_scanned == 0`、警告仅 stderr | `test_json_zero_files_warning_stderr_only` | `json.loads(result.stdout)` + `"warning" not in result.stdout` |
| AC2 | R2 | T2.3 单文件模式不告警 | `test_no_warning_single_file_mode` | `--mode spacing <合规文件>` → stderr 无 `warning` |
| AC2 | R2 | T2.4 有文件目录不告警 | `test_dir_scan_lib_layout_finds_files`（合规分支） | `files_scanned > 0` → stderr 无 `warning` |
| AC3 lib/ 布局真实扫描 | R3 | T3.1 脚本级：违规 `lib/components/Button.tsx` → exit 1、`files_scanned == 1` | `test_dir_scan_lib_layout_finds_files`（违规分支） | `--dir <tmp>/lib --all --json` |
| AC3 | R3 | T3.2 合规树 exit 0 且 `files_scanned > 0` | 同上（合规分支） | 非零扫描计数 |
| AC3 | R3 | T3.3 接线级：配置 `lib` 展开为 `--dir <cwd>/lib`，执行后 `files_scanned > 0` | `test_ui_scan_dir_expansion_default_and_configured`（执行展开后命令） | `expand_cmd` 输出 → `_run(...)` |
| AC4 回归门禁 | R4 | T4.1 `pytest tests/test_ui_check.py` 全绿（12 旧 + 6 新） | — | 含三个锚点测试 |
| AC4 | R4 | T4.2 `make ci` 全绿 | — | check + self-test + test + quality + org |

回归锚点（不允许破坏）：`test_dir_scan_no_frontend_files_passes`（:57-61）、
`test_ui_refactor_pipeline_schema_and_gate`（:102-112）、
`test_json_clean_path_stdout_pure`（:138-149）。

---

## 7. 设计决策与否决项（摘要）

| 决策 | 选择 | 理由 |
|---|---|---|
| 机制形态 | 顶层键 + `{ui_scan_dir}` 占位符 | 与 `{tool_root}` 及现有顶层键模式一致；实现面最小（1 helper + 4 调用点） |
| 展开 helper 位置 | `pbatch/config.py` | 与 `_BATCH_CFG`/`TOOL_ROOT` 同居；runner/repo_gates/cli_batch 均已 import config |
| 否决：validator 映射形式 `{cmd, vars}` | — | 新嵌套 schema 与校验面，超出方向所需 |
| 否决：fork yaml 手改字面量 | — | 正是现状（无信号、易漂移），本方向消除之 |
| 默认值 | 未配置 → `{cwd}/src` | 向后兼容、零迁移；非 src 布局由 R2 警告暴露 |
| 警告通道 | stderr + exit 0 不变 | 不破坏两个 stdout 契约锚点；stderr 恰为重试 prompt 通道 |

Non-goals：不改默认扫描根；不给 uigeometry/uiquality 加警告；不动
`backendquality`；不改扫描语义/CLI 参数/`--json` 契约。

---

## 8. 实现清单（file:line 触点）

- [ ] `pi-batch.yaml:163/165/171` 命令改 `--dir {ui_scan_dir}`；:158-160 注释；新增顶层键示例
- [ ] `pbatch/config.py`：`ui_scan_dir()` + `expand_cmd()`（`_BATCH_CFG` 区域）
- [ ] `pbatch/runner.py:487`、`:698` 换 helper
- [ ] `pbatch/repo_gates.py:31-32` 换 helper（顺带 `{tool_root}`）
- [ ] `pbatch/cli_batch.py:408` 换 helper（`output=None`）
- [ ] `scripts/check-ui-spec.py` `main()` 零文件 stderr 警告
- [ ] `tests/test_ui_check.py` 新增 6 测试（§6 表）
- [ ] `examples/ui-refactor-pipeline.yaml:14` 注释
- [ ] 门禁：`pytest tests/test_ui_check.py` → `make ci` → `quality.py --strict .`
