# Requirements Specification — 统一校验器 CLI 面与 --json 的 stdout 契约（--skip 缺失 + JSON 后跟摘要行污染机器输出）

| Field | Value |
|---|---|
| Module | `scripts` |
| Direction id | `8f645734` |
| Source analysis | `docs/auto/analyses/scripts-8f645734.json` (direction #1) |
| Status | Proposal (evidence-verified) |
| Scope | This direction only — no expansion |

---

## 1. Summary

`scripts/check-backend-quality.py` 与 `scripts/check-frontend-quality.py` 用
手写 `parse_argv`，**不支持 `--skip`**（项目无法排除自己的
generated/迁移/杂项目录，误报只能改代码调优），且 `--json` 时 JSON 文档
输出后**仍向 stdout 打印人类摘要行**，污染机器可读输出：

- `check-backend-quality.py --json` 在 OK 路径打印
  `BACKEND-QUALITY: OK (N file(s) scanned)`（第 466 行，无 `file=sys.stderr`），
  在 skipped 路径打印 `BACKEND-QUALITY: no backend manifest detected; gate skipped`
  （第 464 行，同样落 stdout）。实测两个路径的 stdout 均**无法被
  `json.loads` 直接解析**（`JSONDecodeError: Extra data`）。
- `check-frontend-quality.py --json` 在 OK 路径打印
  `UI-QUALITY: OK (N file(s) scanned)`（第 281 行，落 stdout），同样污染。

consumers 被迫打补丁：`pbatch/health.py:48` 注释
*"check-backend-quality 无 --skip（内置 DEFAULT_EXCLUDES）；其余接受逗号参数"*
并特判 `if name != "check-backend-quality": cmd += ["--skip", SKIP]`；
`pbatch/health.py:57` 用 `json.JSONDecoder().raw_decode` 跳过尾行才解析到
JSON。`pbatch/advance.py:97-108` 的 `_run_checker` 同样用
`json.loads(result.stdout)`，在本仓库上对 backend checker 静默降级为
`{}`（skipped 状态尾行导致解析失败，`files_scanned` 恒为 0）。

同族的 `check-ui-spec.py:244-253` 已正确实现（`--json` 时摘要走 stderr、
stdout 纯 JSON，并提前 return）；`check-design-intelligence.py:274-276` 与
`check-knowledge-freshness.py:225` 已支持 `--skip`（逗号分隔 path 片段），
`pi-batch.yaml:131` 的 designintelligence validator 已在用 `--skip`。因此
本方向修复的是**家族内不一致**（两个手写 parse_argv 的检查器落后于已确立
的 CLI 约定），而非设计约束。

---

## 2. Evidence verification

方向引用的每个证据点均对照当前仓库重新核验（行号以当前文件为准；两处
引用行号不精确，但文件级事实全部属实）。

| 引用证据 | 核验结果 |
|---|---|
| `scripts/check-backend-quality.py:323` | ⚠️ **行号不精确**（实际在 `_warning_findings` 函数体中部），但所指向的文件级事实全部属实：手写 `parse_argv` 在 **:396-413**，只认 `--dir/--manifest/-o/--strict/--json`，**无 `--skip`**；`DEFAULT_EXCLUDES` 在 :64-76。 |
| `scripts/check-backend-quality.py:371` | ⚠️ **行号不精确**（实际是 `check_text` 内语言过滤注释），但所指向的摘要行污染事实属实：`_emit_report` 的 `print(payload)` 在 **:447**（stdout），而 `_finish` 的 OK 摘要 `print(f"BACKEND-QUALITY: OK ({n_files} file(s) scanned)")` 在 **:466**、skipped 摘要 `print("BACKEND-QUALITY: no backend manifest detected; gate skipped")` 在 **:464** —— 两者**均无 `file=sys.stderr`，落 stdout**。 |
| `scripts/check-frontend-quality.py:220` | ✅ **精确**。`def parse_argv(argv: list)` 在 :220-234，只认 `--dir/--strict/--json/-o`，**无 `--skip`**。 |
| `scripts/check-frontend-quality.py:268` | ✅ **精确**。:268 `if want_json or report_path:` 进入 JSON 输出块，`print(payload)` 在 :274（stdout）；OK 摘要 `print(f"UI-QUALITY: OK ({len(paths)} file(s) scanned)")` 在 **:281**，**无 `file=sys.stderr`，落 stdout**。 |
| `scripts/check-ui-spec.py:238` | ✅ **精确**。:238 `if want_json or report_path:`；:244 `print(payload)` 后紧跟 :245 注释 *"stdout carries ONLY the JSON report; summary goes to stderr"*，:246-251 摘要全部 `file=sys.stderr` 并 :252 提前 return —— 即方向所称的"正确实现"参照。 |
| `pbatch/health.py:48` | ✅ **精确**。:48 注释 *"check-backend-quality 无 --skip（内置 DEFAULT_EXCLUDES）；其余接受逗号参数。"*，:49-50 `if name != "check-backend-quality": cmd += ["--skip", SKIP]`；`SKIP = ".git,.dart_tool,test,build,vendor,checks"`（:26）。 |
| `pbatch/health.py:57` | ✅ **精确**。:55-56 注释 *"stdout 可能带尾行（如 \"BACKEND-QUALITY: OK\"）——只解析首个 JSON 对象"*，:57 `decoded, _ = json.JSONDecoder().raw_decode(result.stdout[start:])`。 |

### 实测复现（当前仓库，全部 live）

```
# backend, skipped 路径（本仓库无 backend manifest：无 go.mod/package.json/pom.xml）：
$ python scripts/check-backend-quality.py --dir . --json 2>/dev/null | tail -2
}
BACKEND-QUALITY: no backend manifest detected; gate skipped          # ← 尾行污染

$ python scripts/check-backend-quality.py --dir . --json 2>/dev/null \
  | python -c "import sys,json; json.loads(sys.stdin.read())"
json.decoder.JSONDecodeError: Extra data: line 10 column 1 (char 139)  # json.loads 失败

# backend, OK 路径（仓库内临时 fixture：go.mod + 干净的 main.go，避开 /tmp/go.mod 杂散 manifest）：
$ python scripts/check-backend-quality.py --dir .beq-ok-XXXX --json
{ ... "status": "ok" }
BACKEND-QUALITY: OK (1 file(s) scanned)                                # ← 尾行污染
$ ... | python -c "import sys,json; json.loads(sys.stdin.read())"
json.decoder.JSONDecodeError: Extra data: line 10 column 1 (char 151)  # json.loads 失败

# frontend, OK 路径：
$ python scripts/check-frontend-quality.py --dir examples --json 2>/dev/null | tail -2
}
UI-QUALITY: OK (0 file(s) scanned)                                     # ← 尾行污染

# 对照：violation 路径两者 stdout 已纯（violations/摘要均走 stderr）——
# 污染只在 OK / skipped 路径出现：
$ python scripts/check-backend-quality.py --dir <viol-fixture> --json | json.loads   # PARSE OK, total: 1
$ python scripts/check-frontend-quality.py --dir <viol-fixture> --json | json.loads  # PARSE OK, total: 1
$ python scripts/check-ui-spec.py --dir . --all --json | json.loads                  # PARSE OK, total: 0
```

### 家族约定核验（证明这是可修复的不一致）

- `check-design-intelligence.py:218-226` `_collect_files(root, skip)`：`--skip`
  逗号分隔片段，`any(part in skipped for part in p.parts)` —— **片段按路径段
  相等匹配**；`--skip` 由 argparse 声明（:274-276）。`check-knowledge-freshness.py:225`
  同样支持 `--skip`。
- `pi-batch.yaml:131` designintelligence validator 已在用
  `--skip .dart_tool,test,build,ephemeral`。
- `check-ui-spec.py` 用 `--exclude`（:154-161，单值加入 excludes 集合）——
  不同旗标名但同一"追加排除"语义。
- 两个待修检查器的 `DEFAULT_EXCLUDES` 已内置大量条目：backend（:64-76）
  含 `scripts/tools/checks/tests/docs/generated/migrations/...`；frontend
  （:38-51）含 `e2e/test/tests/spec/...`。因此 `--skip` 的语义必须是**追加
  union，绝不替换**默认集合（否则 `--skip e2e` 会把其余默认排除项全部放开）。

### 受影响的 consumer 全景（全部核实）

| Consumer | 位置 | 现状 |
|---|---|---|
| `pbatch/health.py` | :48-50, :55-57 | `--skip` 特判 + `raw_decode` 尾行跳过 |
| `pbatch/advance.py` | `_run_checker` :97-108 | `json.loads(result.stdout)` —— 本仓库 backend 扫描静默失败返回 `{}`（`files_scanned: 0`），被同一契约缺陷波及（修复自动受益，无需单独改动） |
| `pi-batch.yaml` validators | :171 `uiquality`, :181 `backendquality` | `--json` 调用，靠退出码判定，不受 stdout 污染影响（violation 细节已在 stderr） |
| `tests/test_frontend_quality.py` | :59-68 `test_json_report_and_output_file` | 只覆盖 violation 路径（exit 1）——OK 路径的 stdout 纯度**从未被断言**，这正是缺陷漏网的原因 |
| `tests/test_ui_check.py` | :75-88 | ui-spec violation 路径已断言 `json.loads(result.stdout)` 成功 |

---

## 3. Root-cause analysis

两个结构性原因叠加：

1. **CLI 面分裂**：家族中 argparse 系的检查器（design-intelligence、
   knowledge-freshness）早已确立 `--skip` 约定并被 `pi-batch.yaml`/`health.py`
   依赖；而 backend/frontend 两个检查器保留手写 `parse_argv`，从未跟上该
   约定。`health.py` 不得不为它写 `name != "check-backend-quality"` 特判 ——
   特判即债务：未来任何新增检查器只要缺 `--skip`，特判就得继续长。
   更糟的是，两个手写 parse_argv 对未识别旗标**静默降级为文件 target**
   （实测 `--skip scratch` 把 `--skip` 与 `scratch` 双双计入待扫描文件，
   产生 "missing target" 伪违规并 inflate `files_scanned`，且不报错）——
   缺旗标不是"拒绝"而是"误解析"，排障更困难。
2. **--json 模式下摘要落 stdout**：两个检查器的 `_finish`/`main` 在
   OK/skipped 路径用裸 `print(...)`（无 `file=`）打摘要，与 JSON 文档混在
   同一 stdout。violation 路径恰好安全（摘要早已走 stderr）只是巧合，于是
   现有测试（只测 violation 路径）从未暴露。`check-ui-spec.py` 的
   :244-253 证明正确模式（stdout 纯 JSON + 摘要 stderr + 提前 return）在同
   仓库已存在 —— 这是复制既有正确模式，不是新设计。

修复 = 把两个检查器对齐到家族已有约定（`--skip` 追加排除 + `--json`
stdout 纯 JSON），然后删除 `health.py` 的两个补丁。

---

## 4. Scope boundary

**In scope:**
- `scripts/check-backend-quality.py`、`scripts/check-frontend-quality.py`：
  `parse_argv` 增加 `--skip`（逗号分隔 path 片段，语义与
  `check-design-intelligence.py` 一致：片段按路径段相等匹配、追加 union
  到 `DEFAULT_EXCLUDES`），并统一 `--json` 的 stdout 契约（stdout 只承载
  一个 JSON 文档；全部人类摘要走 stderr）。exit code 与 fail-closed 语义
  不变；非 `--json` 人类模式输出逐字节不变。
- `pbatch/health.py`：删除 `name != "check-backend-quality"` 特判（三个
  checker 统一追加 `--skip SKIP`），`raw_decode` 替换为 `json.loads`；
  超时/returncode/-1 哨兵/报告结构不变（AC4）。
- 新回归测试（§7）：`tests/test_backend_specs.py`、
  `tests/test_frontend_quality.py`、`tests/test_tools_health.py`、
  `tests/test_ui_check.py`，全部 hermetic（沿用 test_backend_specs.py
  的仓库内临时目录约定，规避开发机 `/tmp/go.mod` 杂散 manifest）。
- 验证：`make ci`（387+ 回归测试）+ `python quality.py --strict .` +
  `python -m pytest tests/test_backend_specs.py tests/test_frontend_quality.py
  tests/test_tools_health.py tests/test_ui_check.py`。

**Out of scope（明确不做）：**
- `check-ui-spec.py` 的任何改动（其 `--json` 契约已正确，`--exclude` 旗标
  保留；AC5 只是防回归守卫）。
- 把手写 `parse_argv` 迁移到 argparse（方向只要求旗标语义，不要求解析库；
  最小改动为扩展现有 parse_argv）。
- 增删任一检查器 `DEFAULT_EXCLUDES` 的条目内容（只新增 `--skip` 追加机制）。
- `pbatch/advance.py` 的改动（仅作为契约修复的受益者，不触碰）。
- `check-design-intelligence.py` / `check-knowledge-freshness.py` /
  `check-ui-geometry.py`（已符合约定，不在本方向）。
- 非 `--json` 人类输出、exit code、`-o` 报告文件写入的改动。
- `pi-batch.yaml` validator 命令的改动（uiquality/backendquality 维持现状，
  项目方需要排除时用各自配置传 `--skip`）。

---

## 5. Requirements

**R1（CLI 面）— backend/frontend 检查器支持 `--skip`。**
`scripts/check-backend-quality.py` 与 `scripts/check-frontend-quality.py` 的
`parse_argv` 接受 `--skip NAMES`（NAMES 为逗号分隔的 path 片段，如
`--skip generated,migrations`）。语义与 `check-design-intelligence.py` 的既有
约定一致：片段按**路径段相等**匹配（`any(part in skipped for part in path.parts)`，
非子串匹配，任意深度生效）；排除集 = `DEFAULT_EXCLUDES ∪ skip 片段`
（**追加，绝不替换**默认集合）；`--skip ""` 为 no-op；`--skip` 只影响
`--dir` 目录扫描，显式文件 target 不受影响。backend 的 manifest
walk-up/forced 探测逻辑不变。

**R2（stdout 契约）— `--json` 时 stdout 只承载一个 JSON 文档。**
两个检查器在 `--json` 模式下，stdout 必须能被 `json.loads` 直接解析：
`"BACKEND-QUALITY: OK (...)"`、`"BACKEND-QUALITY: no backend manifest detected;
gate skipped"`、`"UI-QUALITY: OK (...)"` 等全部人类摘要行在 `--json` 时一律
走 stderr（OK/skipped/empty/scan_error/violation 五种状态一致）；
violation 细节行维持 stderr（runner 靠 stderr 喂重试 prompt 的既有契约）。
JSON 文档末尾允许换行（`json.loads` 容忍空白），不变式为"无非 JSON 尾行"。
exit code 不变（0 通过 / 1 拒绝，fail-closed 语义不动）。非 `--json` 模式
与 `-o` 文件写入逐字节不变（对齐 `check-ui-spec.py:244-253` 的既有正确
模式）。

**R3（health.py 补丁删除）— `_checker_violations` 统一走 plain `json.loads`。**
`pbatch/health.py` 删除 :48-50 的 `name != "check-backend-quality"` 特判
（三个 checker 统一 `cmd += ["--skip", SKIP]`），:55-57 的
`raw_decode` + `stdout.index("{")` 替换为 `json.loads(result.stdout)`。
超时（120s）、returncode∈{0,1} 判断、`-1` 哨兵、`health_report` 报告结构
（keys: `governance/score/tests/checkers/quality_violations/specs/healthy`）
全部不变 —— `health --json` 输出与基线逐字段一致（AC4）。

**R4（回归测试）— 按 §7 测试计划落地 AC1-AC5。**
新增测试全部 hermetic：仓库内临时目录 fixture（沿用
`tests/test_backend_specs.py` 的 `clean_repo` 约定，mkdtemp 于仓库根下，
避开 `/tmp` 杂散 manifest）；backend fixture 自带 `go.mod` 以强制 status
`ok`（否则走 skipped 路径，无法覆盖 OK 摘要行）。测试断言以
`json.loads(result.stdout)` 成功 + 摘要行出现于 stderr + `files_scanned`
计数为主，不依赖具体 violation 模式文本。

**R5（可选，非阻塞）— 文档注记。**
在 `scripts/` 对应文件 docstring 或 `AGENTS.md` 校验器小节补一句：
backend/frontend 检查器现支持 `--skip`（逗号分隔目录片段，追加排除），
与 design-intelligence 一致。不要求任何验收检查；可跳过。

---

## 6. Acceptance criteria (supplied, made testable)

五条 supplied 验收检查原意全部保留；每条钉死为可在 tests/ 自动化的确定性
断言。

**AC1 — `python scripts/check-backend-quality.py --dir . --json` 的 stdout
能被 `json.loads` 直接解析（无尾行）。**
> python scripts/check-backend-quality.py --dir . --json 的 stdout 能被
> json.loads 直接解析（无尾行）

可测形式（`tests/test_backend_specs.py` 新增
`test_backend_json_stdout_contract`，三个 fixture 覆盖全部状态路径）：
- OK 路径：仓库内临时目录 + `go.mod` + 干净 `src/main.go` →
  `--dir fixture --json` exit 0，`json.loads(result.stdout)` 成功且
  `payload["status"] == "ok"`、`payload["total"] == 0`；摘要行
  `"BACKEND-QUALITY: OK"` 出现在 **stderr**，stdout 无 `"BACKEND-QUALITY"`
  字样。
- skipped 路径：仓库内临时目录无 manifest → exit 0，
  `json.loads` 成功且 `payload["status"] == "skipped"`；
  `"gate skipped"` 摘要出现在 stderr。
- violation 路径（防回归）：fixture 带违规 `.go` 文件 → exit 1，
  `json.loads` 成功，`payload["total"] >= 1`，violation 细节与拒绝摘要
  在 stderr。

**AC2 — `python scripts/check-backend-quality.py --dir . --skip
generated,migrations` 不扫描被排除目录。**
> python scripts/check-backend-quality.py --dir . --skip generated,migrations
> 不扫描被排除目录

可测形式（`test_backend_skip_flag`）：注意 `generated`/`migrations` 已在内置
`DEFAULT_EXCLUDES`（:64-76），故测试必须同时验证机制与幂等性：
- 机制：fixture（`go.mod` + `src/a.go` + `scratch/b.go`，`scratch` **不在**
  默认排除集）→ 默认 `files_scanned == 2`；`--skip scratch` →
  `files_scanned == 1` 且 `total` 相应下降（证明追加排除生效）。
- 幂等/原样验收：`--skip generated,migrations` 与默认运行的
  `files_scanned` 一致（已排除目录被再次点名不改变结果），且命令正常
  exit 0、JSON 可解析。
- 空值：`--skip ""` 与默认运行结果一致（no-op）。

**AC3 — `python scripts/check-frontend-quality.py --dir . --skip e2e` 生效。**
> python scripts/check-frontend-quality.py --dir . --skip e2e 生效

可测形式（`tests/test_frontend_quality.py` 新增 `test_frontend_skip_flag`）：
`e2e` 已在内置 `DEFAULT_EXCLUDES`（:38-51），同样验证机制 + 幂等：
- fixture：`src/a.tsx` + `scratch/b.tsx` + `e2e/c.tsx`（各含违规样本，
  如 `const x: any = 1`）→ 默认 `files_scanned == 2`（e2e 已排除）；
  `--skip scratch` → `files_scanned == 1`（机制生效）；
  `--skip e2e` → `files_scanned == 2`（与默认一致，证明 e2e 持续排除、
  旗标被接受且无副作用）；`--skip scratch,e2e` → `files_scanned == 1`。
- 追加语义反证：`--skip e2e` 后 `node_modules` 等默认排除项**仍被排除**
  （若实现成"替换默认集合"，files_scanned 会异常膨胀 —— 断言
  `files_scanned <= 默认值` 即可兜住）。

**AC4 — `pbatch/health.py` 的 raw_decode 特判与 --skip 分支可删除且
`health --json` 输出不变。**
> pbatch/health.py 的 raw_decode 特判与 --skip 分支可删除且 health --json
> 输出不变

可测形式（`tests/test_tools_health.py` 新增
`test_health_checker_plain_json_contract`）：
- `_checker_violations("check-backend-quality")` 的返回值与"直接以
  `--dir ROOT --json --skip SKIP` 子进程运行并用 `json.loads` 解析"所得
  `total` **相等**（内部一致性：删除 raw_decode 后解析路径即 plain
  `json.loads`）。本仓库基线值为 `0`（skipped 状态）。
- `health_main(["--json"])` exit 0；`json.loads` 解析成功；
  `sorted(report.keys())` == `["checkers","governance","healthy",
  "quality_violations","score","specs","tests"]`（基线 shape 不变）；
  `report["checkers"]["backendquality"]` 为 int。
- 源码层守卫（可选断言）：`pbatch/health.py` 不再含 `raw_decode`。

**AC5 — `python scripts/check-ui-spec.py --dir . --json` 现有行为不回归。**
> python scripts/check-ui-spec.py --dir . --json 现有行为不回归

可测形式（`tests/test_ui_check.py`）：
- 既有 `test_json_report_and_output_file`（:75-88，violation 路径）与
  `test_exclude_pattern` 等全套用例不改动、全绿。
- 新增 `test_json_clean_path_stdout_pure`：干净 fixture →
  `--all --json` exit 0，`json.loads(result.stdout)` 成功，
  `payload["total"] == 0`；`"UI-SPEC"` 摘要出现在 stderr。

---

## 7. Test plan (mapping AC → concrete tests)

| Test | File | What it does |
|---|---|---|
| `test_backend_json_stdout_contract` (AC1) | `tests/test_backend_specs.py` | 三个仓库内 fixture（ok/skipped/violation）；每种状态 `json.loads(stdout)` 成功、摘要行在 stderr、exit code 正确。 |
| `test_backend_skip_flag` (AC2) | `tests/test_backend_specs.py` | fixture `src/`+`scratch/`：默认 `files_scanned==2`；`--skip scratch` → 1；`--skip generated,migrations` 与默认一致；`--skip ""` no-op。 |
| `test_frontend_skip_flag` (AC3) | `tests/test_frontend_quality.py` | fixture `src/`+`scratch/`+`e2e/`：默认 2；`--skip scratch` → 1；`--skip e2e` 与默认一致；追加语义反证（默认排除项不被放开）。 |
| `test_frontend_json_ok_path_pure` (R2 覆盖) | `tests/test_frontend_quality.py` | 干净 fixture：`--json` exit 0，`json.loads(stdout)` 成功，`"UI-QUALITY: OK"` 在 stderr（方向问题陈述点名 frontend 的同一污染；supplied AC1 只列 backend，此测试补足方向标题"统一 stdout 契约"的另一半）。 |
| `test_health_checker_plain_json_contract` (AC4) | `tests/test_tools_health.py` | `_checker_violations("check-backend-quality")` == 直接运行 + `json.loads` 的 total；`health_main(["--json"])` shape/keys 与基线一致、exit 0；无 `raw_decode`。 |
| `test_json_clean_path_stdout_pure` (AC5) | `tests/test_ui_check.py` | 干净 fixture `--all --json`：exit 0，stdout 纯 JSON，摘要走 stderr；既有 ui-spec 用例全绿。 |
| 仓库门禁 | CI | `make ci`（387+ 回归测试 + 25 eval）+ `python quality.py --strict .` 全绿。 |

所有新测试沿用现有风格（subprocess 运行 `sys.executable SCRIPT ...` +
`json.loads` 断言，见 `tests/test_backend_specs.py:34-36` 的 `_run` 助手）；
fixture 一律 mkdtemp 于仓库根下，不触碰真实 `build/`/`docs/` 等目录。

---

## Appendix A — live verification log (this repository)

```
# 1. backend --json skipped 路径污染（本仓库无 backend manifest）
$ ls go.mod package.json pom.xml   # 均不存在
$ python scripts/check-backend-quality.py --dir . --json 2>/dev/null | tail -2
}
BACKEND-QUALITY: no backend manifest detected; gate skipped
$ python scripts/check-backend-quality.py --dir . --json 2>/dev/null \
  | python -c "import sys,json; json.loads(sys.stdin.read())"
json.decoder.JSONDecodeError: Extra data: line 10 column 1 (char 139)

# 2. backend --json OK 路径污染（仓库内 fixture：go.mod + 干净 main.go）
$ python scripts/check-backend-quality.py --dir .beq-ok-XXXX --json
{ "strict": false, "directory": ".beq-ok-XXXX", "files_scanned": 1,
  "violations": [], "total": 0, "manifest": "go.mod", "status": "ok" }
BACKEND-QUALITY: OK (1 file(s) scanned)
$ python scripts/check-backend-quality.py --dir .beq-ok-XXXX --json 2>/dev/null \
  | python -c "import sys,json; json.loads(sys.stdin.read())"
json.decoder.JSONDecodeError: Extra data: line 10 column 1 (char 151)

# 3. frontend --json OK 路径污染
$ python scripts/check-frontend-quality.py --dir examples --json 2>/dev/null | tail -2
}
UI-QUALITY: OK (0 file(s) scanned)

# 4. 对照：violation 路径已纯（摘要走 stderr），ui-spec 双路径已纯
$ python scripts/check-backend-quality.py --dir <viol> --json 2>/dev/null | json.loads   # OK
$ python scripts/check-frontend-quality.py --dir <viol> --json 2>/dev/null | json.loads  # OK
$ python scripts/check-ui-spec.py --dir . --all --json 2>/dev/null | json.loads          # OK

# 4b. 补充实测（"不支持 --skip" 的精确后果）：未识别旗标被静默当作文件
#     target —— '--skip' 与 'scratch' 均按 "missing target" 计入违规，
#     既不拒绝也不排除，files_scanned 被 inflate：
$ python scripts/check-backend-quality.py --dir <viol-fixture> --json | json.loads
#   → files_scanned: 2, total: 1
$ python scripts/check-backend-quality.py --dir <viol-fixture> --skip scratch --json | json.loads
#   → files_scanned: 4, total: 3   ← '--skip' 与 'scratch' 被当作待扫描文件
$ python scripts/check-frontend-quality.py --dir <fixture> --skip scratch --json | json.loads
#   → files_scanned: 4, violations 含 '--skip'、'scratch' 两个伪文件路径

# 5. health --json 基线（修复后必须逐字段不变）
$ python pbatch/health.py --json
keys: ['checkers','governance','healthy','quality_violations','score','specs','tests']
checkers: {'designintelligence': 0, 'knowledge': 0, 'backendquality': 0}   # backendquality == 0
healthy: True

# 6. 家族约定：--skip 已是既有 CLI 面
$ grep -n "skip" scripts/check-design-intelligence.py | head -3
218: def _collect_files(root: Path, skip: str) -> list[Path]:
274: "--skip",
$ grep -n '"--skip"' pi-batch.yaml
131: designintelligence: "python {cwd}/scripts/check-design-intelligence.py --dir {cwd} --json --skip .dart_tool,test,build,ephemeral"
```

**行为基线（修复前后必须保持）**：
- 非 `--json` 人类模式输出逐字节不变（摘要仍在 stdout）。
- exit code 不变：0 通过 / 1 拒绝；backend `--strict`、manifest
  walk-up/forced 探测、frontend `--strict` 警告分级逻辑全部不动。
- violation 细节行维持 stderr（validator runner 重试 prompt 依赖）。
- `health --json` 报告 keys/shape/数值（本仓库 backendquality=0）不变。
- 无 manifest 时 backend 的 `status: "skipped"` 行为不变，只是摘要行改道
  stderr。
