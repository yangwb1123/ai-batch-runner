All measurements complete. Here is the QA lead review.

---

# QA Lead Review — `scripts-8f645734`（--skip + --json stdout 契约）

**关键上下文（Verified）**：本次评审期间，campaign runner（PID 1547473 等 4 个并发 campaign）已在工作区落地了该方向的设计实现。本报告同时评审**设计测试计划**与**已落地实现**（git status 显示 `M scripts/check-{backend,frontend}-quality.py`、`M pbatch/health.py`、新测试全部就位）。所有污染复现均在我首轮读取时（实现落地前）对旧代码完成。

## 1. Test inventory — 本修订实际运行的命令

| 命令 | 结果 | 证据 |
|---|---|---|
| `python -m pytest tests/test_backend_specs.py tests/test_frontend_quality.py tests/test_tools_health.py tests/test_ui_check.py -q` | **100 passed**（91 基线 + 9 新增；设计预计 8，实际多 1：`test_health_main_json_shape`） | 19.43s |
| 9 个新测试单独 -q | **9 passed** | 1.68s |
| `python pi-batch.py eval` | **all 42 passed** | 规则回归套件 |
| `python quality.py scripts/check-backend-quality.py scripts/check-frontend-quality.py pbatch/health.py` | **quality: OK** | 组织门禁 |
| `python -m pbatch.health --json` | `checkers: {di:0, k:0, be:0} healthy: True` | 新代码端到端 |
| 15+ 次 live CLI 探针（见 §3/§4） | 见各条 | 前后对比 |

**未运行（明确归因）**：完整 `make ci`（check/self-test/test/quality/org）。当前 4 个 campaign 正在并发写同一棵树，此刻全量跑会把其他方向的中途状态混入本方向的验收。**退出标准要求 settle 后全量重跑。**

## 2. Requirement-to-test matrix

| 验收 | 测试（文件） | 状态 | 证据 |
|---|---|---|---|
| AC1 backend `--json` stdout 纯 JSON（ok/skipped/violation 三态） | `test_backend_json_stdout_contract` | ✅ PASS | 三 fixture 均严格 `json.loads`；`"BACKEND-QUALITY" not in stdout`；exit 0/0/1；stderr 摘要 |
| AC2 backend `--skip` union 语义 | `test_backend_skip_flag` | ✅ PASS | 默认 2 → `--skip scratch` 1；`generated,migrations` 幂等 2；`--skip ""` no-op 2 |
| AC3 frontend `--skip` + 反证 | `test_frontend_skip_flag` | ✅ PASS | 默认 2（e2e 默认排除）；`--skip e2e` 仍 2（**replace 实现会得 3**，负控制成立）；`scratch,e2e`→1 |
| AC4 health raw_decode 可删 + `--skip` 统一 | `test_health_checker_plain_json_contract` + `test_health_main_json_shape` | ✅ PASS | 与直跑子进程相等；源码守卫 `raw_decode`/`JSONDecoder` 不在、`"--skip", SKIP` 统一；keys 排序匹配、`backendquality` int、exit 0 |
| AC5 ui-spec 不回归 | `test_json_clean_path_stdout_pure` | ✅ PASS | exit 0、`json.loads` OK、`UI-SPEC` 在 stderr 不在 stdout |
| R2 frontend OK 路径纯度（方向标题的另一半） | `test_frontend_json_ok_path_pure` | ✅ PASS | exit 0、`json.loads` OK、`UI-QUALITY` 在 stderr 不在 stdout |
| C1 非 json 逐字节不变 | `test_backend_non_json_unchanged`（backend）；frontend 由既有 `test_clean_file_passes`/`test_empty_dir_passes`（非 json 断言 stdout）兜住 | ✅ PASS | OK 行在 stdout、无 `{` |
| C2 `-o`-only 不变 | `test_backend_o_flag_summary_on_stdout`；frontend **未 pin**（见 F6） | ⚠️ 半覆盖 | backend ✅；frontend 仅 live 验证 |
| C5 显式文件目标不受 `--skip` 影响 | **无测试** | ❌ GAP | live 验证 `--skip scratch <scratch/下的文件>` 仍报违规（F2） |
| F3 尾部 `--skip`（无值） | **无测试** | ❌ GAP | live 验证 `--json --skip` → exit 1、`'--skip'` phantom 违规、可见非静默（F1） |
| 全排除 fail-closed（backend） | **无测试** | ❌ GAP | live 验证 `--skip src` → `status: empty`、exit 1（F3） |
| scan_error 路径 stdout 纯度 | **无测试**（既有 `test_backend_checker_scan_error_json` 仍用容忍式 raw_decode） | ❌ GAP | live 验证已纯（F4） |

## 3. Findings（按严重度）

**F1 — Medium — `--skip` 吞旗标（静默降级）**。live 验证（两 checker 同）：`--dir X --skip --json` → `--skip` 把 `--json` 当值吞掉，**静默退回 human 模式**（stdout 无 JSON、exit 0）；`--skip --strict` → `strict: False` 静默丢失。设计 F3 只描述了"尾部无值"情形（该情形实测工作正常：`--json --skip` → exit 1、`'--skip'` missing-target 违规，可见非静默），未覆盖"后随旗标"的吞值。**回归风险**：agent/用户笔误得到错误模式且 clean 输入下 exit 0 → 假通过。属既有类行为（`--dir --strict` 同款、argparse 家族同款），当前无任何存量调用命中。**要加的测试**：`test_backend_trailing_skip_diagnostic`（`--json --skip` → exit 1、JSON 含 `'--skip'` missing target）+ 文档一行注明后随旗标会被吞值。

**F2 — Medium — C5 零覆盖**。设计把"显式文件目标不受 `--skip` 影响"列为约束 C5，测试计划未 pin。live 验证实现正确：`--skip scratch <scratch/b.tsx>` → 该文件违规照报（files_scanned=1、total=1）；positional 模式（无 `--dir`）`--skip` 为 no-op。**要加的测试**：两 checker 各一：`--skip scratch <显式文件>` → 违规仍出现在 JSON；`--skip scratch file.go`（无 `--dir`）→ 结果与不带 `--skip` 逐字相同。**断言**：`violations` 含该文件、`total` 不降。

**F3 — Medium — 全排除 fail-closed 未 pin**。backend `--skip src`（go.mod 仓库全部源码被排）→ `status: empty`、exit 1（live 验证）——这是 union 语义下"激进 --skip 不能制造空洞通过"的关键安全属性，值得一条断言固定。frontend 对应面（`--skip src,scratch` → 0 文件、exit 0）与 frontend 空目录约定一致（既有 `test_empty_dir_passes` 同款），把不对称性**显式钉住**为设计意图。

**F4 — Low — scan_error 路径纯度未 pin**。live 验证 `--dir /nonexistent --json` → stdout 纯 JSON（exit 1）。但既有 `test_backend_checker_scan_error_json`（:303-308）仍用容忍式 `raw_decode`——新契约下这条路径的严格性无人守卫。**强化**：该测试改为严格 `json.loads(result.stdout)`（污染回归会让它红）。

**F5 — Low — AC4 断言同义反复**。`_checker_violations` == 直跑子进程 total，两侧共享同一解析逻辑，系统性解析错误（如读错字段）不会暴露。缓解：`test_health_main_json_shape` 的 exit 0 间接 pin 了 `-1` 哨兵（healthy 要求 all==0）。建议补显式 `assert expected == 0`（本仓库 backend 为 skipped→0）——注意这依赖仓库根不出现 go.mod，正是 fixture 躲避的 walk-up 陷阱，若加需注释该依赖。

**F6 — Low — frontend `-o`-only（C2）无镜像测试**。backend 有 `test_backend_o_flag_summary_on_stdout`，frontend 同构造（live 验证：`-o` 无 `--json` → 摘要留 stdout、报告文件可解析）无测试。补镜像一条。

**F7 — Info — `--skip` 值不去空白**：`"scratch, vendor"` 的 `" vendor"` 段永不命中；与家族（design-intelligence `_collect_files`）一致。文档注明即可，非缺陷。

**F8 — Info — 并发写树/flake 风险**：4 个 campaign 进程（PIDs 225405/225409/1547473/1875635）正在写同一工作区（72 文件未提交、多 untracked 测试文件）。`test_health_main_json_shape` 跑 `health_report()` 全量（quality.py 对**活树**要求 0 违规）——若并发方向落入中间代码会闪红（本次实测通过）。另：`check-ui-spec.py` 的 ui_tokens 改动属另一方向（C8"untouched"按本方向范围成立），AC5 测试在两者共存下通过。**建议**：settle 后重跑全量门禁再落 commit。

## 4. Prioritized scenario list

**Happy**：① backend ok（go.mod+clean）② backend skipped（无 manifest）③ frontend clean `--json` ④ ui-spec clean `--all --json` —— 全部已 pin ✅
**Boundary**：⑤ `--skip ""` no-op ✅ ⑥ `--skip` 重复排除幂等 ✅ ⑦ **尾部 `--skip` 诊断（F1，未 pin）** ⑧ **`--skip` 后随旗标吞值（F1，未 pin/未文档化）** ⑨ **全排除：backend fail-closed / frontend 空通过（F3，未 pin）** ⑩ **显式文件目标不受影响（F2，未 pin）** ⑪ 段精确匹配 vs 根文件同名（F9，设计已文档化，`scratch.go` 根文件不被排除）⑫ 空白/绝对路径段（F7，Info）
**Error**：⑬ scan_error+`--json` 纯度（F4，未 pin）⑭ missing target 违规（既有测试覆盖）⑮ 不可读文件（既有路径）
**Race**：⑯ 纯子进程、无共享状态、扫描排序确定性 —— 无竞态面；health 120s 超时未变；⑰ 并发 health 运行只读无副作用（read-only）
**Recovery**：⑱ health 未来 checker stdout 回归 → `json.loads` ValueError → `-1` → `healthy: False`（F4 设计兜底，live 验证三 checker 全路径已纯，含 **violation 路径**——我额外构造真实违规 fixture 验证 design-intelligence/knowledge-freshness 的 strict 纯度，设计只验证了 OK/empty 路径）⑲ gate stderr 重试通道 C4 未动 ✅ ⑳ `advance.py` 自动受益（F7 设计声明；本仓库 backend 仍 skipped→0，"变非零"仅对带 manifest 的目标目录成立——表述准确）

## 5. CI/manual-suite gaps、flake 风险、fixtures、exit criteria

- **缺口**：F1/F2/F3/F4/F6 共 5 条未 pin（均为低成本单断言测试）；frontend 尾部 `--skip` 无镜像。
- **Flake 风险**：① `test_health_main_json_shape` 依赖活树全绿（并发写树期间脆弱，实测当前绿）② 若加 F5 的 `== 0` oracle，仓库根出现 go.mod 即红（须注释）③ `clean_repo` 在仓库根 mkdtemp（teardown 清理；与既有约定一致，git status 瞬时噪音）。
- **Fixtures**：设计两个陷阱处理正确并已落地——repo 内 mkdtemp 规避 `/tmp/go.mod` walk-up；`.go`-only fixture 规避"`.py` 在 go.mod 下不被扫"的数值偏差（0→2 vs 2→4，设计 refinement #1 兑现）。
- **Exit criteria**：settle 后 `make ci` 全量绿（含 42 eval，实测已绿）+ `quality.py --strict .` 全树 + 本报告 F1-F4/F6 五项补测落地后复跑 4 文件。

**结论**：实现与设计一致（API-1/2/3 逐条吻合，含 `file=sys.stderr if want_json else None` 构造与 `-o`-only 条件），9 个新增测试全部实测通过，无既有测试需要修改（frontend 测试文件改动为纯新增，diff 0 删除行）。设计与实现均可接受，附 5 项补测建议（F1-F4/F6，合计约 40 行）+ 2 项文档注记（F1 吞旗标、F7 空白段）。
