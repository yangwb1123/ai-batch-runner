# 功能需求分析 — ai-batch-runner（2026-08-21）

> 基于当前代码库（pbatch/ 152 模块 ≈35k LOC、86 测试文件、`pi-batch --help`
> 全部参数、docs/feature-matrix.md、docs/requirements/STATUS.md）的静态分析。
> 需求点按功能域编号；「状态」指当前代码证据，不是承诺。

## 1. 项目定位

单一事实源：**面向任意 CLI 编码 agent（pi / Claude Code / Codex / Gemini /
OpenCode）的治理化批处理/流水线运行时 + 交互式终端工作台**。本地优先、不绑定
单一厂商；AI 产出是**提案**，须经验证器/审核门禁后才落盘。

分层架构（docs/ARCHITECTURE.md）：CLI/TUI 入口 → 任务执行引擎 → 流水线编排 →
规则/评估/记忆/治理 → 设备织网（可选）→ 工程门禁（make ci 体系）。

## 2. 功能域与需求点

### F1 交互入口层（产品 CLI）
| 编号 | 需求点 | 证据 | 状态 |
|---|---|---|---|
| FR-1.1 | 无参数真实终端默认启动 Textual 交互工作台（对话式 Markdown、流式输出、状态侧栏、命令面板、/命令、Ctrl+P/N/L/C/Q） | `pbatch/tui*.py`、CHANGELOG 0.3.0 | ✅ |
| FR-1.2 | stdin/stdout 任一重定向时严格回退无头契约（pipe/-p/pipeline/子命令/JSON 兼容不变） | CHANGELOG 0.3.0「兼容性」 | ✅ |
| FR-1.3 | 环境诊断 `doctor`（安装/目录/配置分层/agent/密钥/锁） | `pbatch/health.py`、`doctor` 子命令 | ✅ |
| FR-1.4 | 用户级 XDG 目录骨架 `init`/`version`/`clean`/`completion`（注册表驱动补全） | CHANGELOG 0.2.0 | ✅ |
| FR-1.5 | shell 补全注册表随新子命令自动同步 | `pbatch/tools.py` 命令索引 | ✅ |

### F2 任务执行引擎（runner）
| 编号 | 需求点 | 证据 | 状态 |
|---|---|---|---|
| FR-2.1 | 多种输入：`-p`/stdin/YAML tasks/`--from-dir`/pipeline `from_prompt`/`from_dir`/`from_outputs` DAG 串联 | `pbatch/task_loading.py`、`from_dir.py` | ✅ |
| FR-2.2 | 硬超时（进程组 SIGKILL、绝对截止、零孤儿）、失败签名拒绝落盘（额度/速率/网络/CLI 横幅，宽泛词不误杀） | `pbatch/runner.py`、`process_exec.py` | ✅ |
| FR-2.3 | 验证门禁：31 验证器注册表、`{output}`/`{tool_root}` 占位符、文件级 4 路并行 + 仓库级阶段末执行、临时文件原子 rename、失败零残留 | `pi-batch.yaml`、`pbatch/validation_batch.py` | ✅ |
| FR-2.4 | 被拒产物有界摘录（64KiB）留 `.pi-batch/rejected/` 供诊断 | runner + CHANGELOG | ✅ |
| FR-2.5 | 重试（指数退避 + jitter）、轮循环 `--max-rounds`、成功节流 `--min-interval` | `pbatch/runner.py`、`cli_rounds.py` | ✅ |
| FR-2.6 | 会话模式 new/shared/per-stage + 稳定可重现 session id + 渐进式 message memory（有界索引、find/recent/read、tenant/sensitivity 边界、脱敏） | `pbatch/memory*.py`、`index_io.py` | ✅ |
| FR-2.7 | 复用控制：`--reuse` 验证器复验、fingerprint sidecar（输入+配置+agent+仓库状态）、gate 产物裁决绑定不复用 | `pbatch/reuse.py` | ✅ |
| FR-2.8 | 治理参数：预算（task/stage/pipeline/campaign 硬截止）、provider 令牌桶限速、项目单实例锁、熔断/停滞分诊、webhook 事件 | `pbatch/ratelimit.py`、`triage.py`、`events.py` | ✅ |
| FR-2.9 | 并行批次安全：重复/嵌套输出拒绝、cwd 输出目录包含关系拒绝、`run_parallel(waves=)` 波次屏障 | runner | ✅ |
| FR-2.10 | 7×24 运营：轮转日志、`--webhook`、E2BIG 前 prompt 硬门（120KiB）、argv 安全 | `pi-batch.yaml` `prompt.max_bytes` | ✅ |
| FR-2.11 | 结构化产物 JSON Schema 门禁（纯 stdlib 子集、未知关键字/$ref 外联 fail closed） | `pbatch/json_schema.py`、`json_schema_cli.py` | ✅ |

### F3 流水线编排（pipeline）
| 编号 | 需求点 | 证据 | 状态 |
|---|---|---|---|
| FR-3.1 | 阶段 DAG：单/多 `from_outputs`、`aggregate` 多源合并、阶段命令（`commands` 硬截止） | `pbatch/pipeline.py` | ✅ |
| FR-3.2 | meta 动态角色编排：JSON 角色计划（命名/ad-hoc）、并发独立会话、相关性评分+运行时上限、证据折叠、role_dir 防穿越/sanitize | `pbatch/relevance.py`、pipeline | ✅ |
| FR-3.3 | gate 裁决门：VERDICT PASS/FAIL/REJECT 解析、FAIL 阻断、裸 PASS fail closed | `pbatch/gate_fix.py`、`gate_contract.py` | ✅ |
| FR-3.4 | gate-fix 自动修复循环：结构化发现（```json）→ 修复任务 + 门禁 → 重审，最多 N 轮 | `pbatch/gate_fix.py` | ✅ |
| FR-3.5 | 有界证据注入：`evidence.max_bytes/max_sources`、公平分配、截断标记保留完整路径 | `pbatch/evidence.py` | ✅ |
| FR-3.6 | 决策日志追加式记录 + 归档（全成功才归档，gate FAIL 保留现场）+ 阶段完成即 git commit | `pipeline_decisions.py`、`pipeline_archive.py` | ✅ |
| FR-3.7 | 启动前 fail-closed schema 检查（阶段名/输入源/validator/正数 worker/meta 上限） | `pbatch/pipeline_schema.py` | ✅ |
| FR-3.8 | 路径隔离：worktree 隔离执行 `isolate`（SSH 会话=worktree）、session 状态机、`--auto-merge` Merge Controller（fetch→rebase→门禁→no-ff/squash→清理）、合并队列=根锁 | `pbatch/isolate.py`、`session_merge.py` | ✅ |

### F4 规则/需求智能层
| 编号 | 需求点 | 证据 | 状态 |
|---|---|---|---|
| FR-4.1 | 任务类型判断 classifier（双语关键词零 LLM 成本，前端/后端路由） | `pbatch/classifier.py`、`classify_route.py`、`task_keywords.yaml` | ✅ |
| FR-4.2 | 需求评估 assess：8 维完整性、规模信号 S/M/L 克制处方、刻意未选清单、双向校验（`--llm-json`） | `pbatch/assessor.py` | ✅ |
| FR-4.3 | 规则匹配器：scale×page_type×risk 三轴 + 算法/LLM 双向校验、必选规则不可跳过、三域注册表 | `pbatch/rule_matcher.py`、`ui-specs/rules.yaml`、`backend-specs/`、`product-specs/` | ✅ |
| FR-4.4 | 实战规律引擎 evolution：项目形态分类、规律分级路由（invariant/contract-policy/suggestion）、探索豁免、项目宪法、门禁五件套、track 演化路径 | `pbatch/evolution.py`、`evolution-specs/` | ✅ |
| FR-4.5 | 上下文路由 context：路径 glob + 关键词加载 ADR/契约/安全基线，缺失显式警告 | `pbatch/context.py`、`context_inject.py` | ✅ |
| FR-4.6 | 自演进闭环 learn：事故/RCA → Rule Schema 草案 → 影子命中率 → 人工批准并入注册表 | `pbatch/learn.py` | ✅ |
| FR-4.7 | 回归保护 eval：42 断言用例随 make ci 自动执行 | `pbatch/eval.py`、`evals/` | ✅ |

### F5 产品思维层与工程纪律
| 编号 | 需求点 | 证据 | 状态 |
|---|---|---|---|
| FR-5.1 | productization 分级 L0-L3 推演链（登录/上传/审批/聊天/ERP/搜索场景）、开源/商业要素、完成证据报告（禁止伪造验证） | `pbatch/product.py`、`product-specs/` | ✅ |
| FR-5.2 | 后端强制关卡：persistence-modeling 编码前 Persistence Design 报告、production-readiness 11 项、架构宪法 15 问 | `backend-specs/` | ✅ |
| FR-5.3 | 前端确定性门禁：8pt token/魔法间距/硬编码颜色/inline style 机械拦截（uispacing/uicolor/uistyle/uifont/uicheck） | `scripts/check-ui-spec.py` | ✅ |
| FR-5.4 | 前端工程启发式门禁：console.log/any/空 catch/测试 skip 恒失败，strict 下上帝文件/复杂度/useEffect 泄漏/N+1 | `scripts/check-frontend-quality.py` | ✅ |
| FR-5.5 | 后端工程门禁：单实现接口/Base 类/反向依赖/上帝服务/N+1/浮点金额/SELECT * | `scripts/check-backend-quality.py`、`backendquality` 验证器 | ✅ |
| FR-5.6 | 诚实完成报告 completion 门禁：result 枚举、not_executed 必带原因 | `scripts/check-completion-report.py` | ✅ |

### F6 治理与知识循环
| 编号 | 需求点 | 证据 | 状态 |
|---|---|---|---|
| FR-6.1 | 决策事务状态机、假设-保证契约、波次调度+图操作、类型化超图 | `transaction.py`、`contracts.py`、`waves.py`、`hypergraph.py` | ✅ |
| FR-6.2 | 真值维护/因果假设/证据衰减/Pinned 上下文/决策胶囊（版本化重放）/N-version 一致性 | `truth.py`、`causality.py`、`temporal.py`、`pinned.py`、`capsule.py`、`nversion.py` | ✅ |
| FR-6.3 | 世界状态 WAL+快照/绑定/对账/漂移、五类指标、交互事件账本、回滚决策+错误预算 | `world.py`、`metrics.py`、`events.py`、`recovery.py` | ✅ |
| FR-6.4 | 量化画像与路由：atomize/profile/pareto/racing/proposal/diversity（零 LLM 成本） | `atomize.py`、`pareto.py`、`racing.py`、`proposal.py`、`diversity.py` | ✅ |
| FR-6.5 | 工程知识循环：Change Impact 增量路由、ADR 决策记忆库、Project Memory（facts/架构/索引） | `impact.py`、`adr.py`、`project_memory.py` | ✅ |
| FR-6.6 | 二阶反思 reflect（12 维 + 假设审计 + 反方验证）、God File 重构分析器、自动改进动作 retro | `reflect.py`、`refactor.py`、`retro.py` | ✅ |
| FR-6.7 | 工程组织规范 org：17 流程节点岗位说明书、门禁执行、{log} 注入、残留占位符 fail closed | `pbatch/org.py`、`agent-engineering/*.yaml` | ✅ |

### F7 自推进迭代（campaign/advance）
| 编号 | 需求点 | 证据 | 状态 |
|---|---|---|---|
| FR-7.1 | 项目级 campaign：模块发现→结构化方向评分（JSON: 问题/价值/成本/证据/验收标准）→Top N 完整实现 | `pbatch/campaign*.py` | ✅ |
| FR-7.2 | 指纹复用（模块内容+仓库快照+分析 prompt+方向+pipeline+角色+runner 配置+model） | `campaign_pipeline.py`、`campaign_state.py` | ✅ |
| FR-7.3 | 并行实现：worktree 隔离、implementation.timeout 总截止、状态事件记录 branch/commit 证据、`--dry-run` 历史耗时估算 | `campaign_worktree.py`、`campaign_runtime.py` | ✅ |
| FR-7.4 | advance 自推进引擎：规范维度扫描→细颗粒分批（P0 可恢复 > P1 token > P2 架构）→state.jsonl 逐轮可续 | `pbatch/advance.py` | ✅ |

### F8 设备织网（AADM-D，默认关闭）
| 编号 | 需求点 | 证据 | 状态 |
|---|---|---|---|
| FR-8.1 | 设备清单/探测/SSH 受控执行、控制平面 serve + 出站 Runner（TLS/mTLS 证书指纹绑定） | `fabric*.py`（20+ 模块）、`docs/AADM_DEVICE.md` | ✅ |
| FR-8.2 | 任务级 Capability Grant（设备/任务/目录/数据级别/有效期）、fencing 握手、主体授权 | `capability_grants.py`、`fabric_exec.py` | ✅ |
| FR-8.3 | 调度：设备/OS/集群/gang/区域/数据级别/模型缓存感知放置、迁移/检查点/投机执行/预算上限/退役吊销/联邦配额 | `fabric_sched.py`、`fabric_fed.py`、`fabric_checkpoint_store.py` | ✅ |
| FR-8.4 | Runner 证据离线缓冲/重放、幂等资源账本、LOST 尝试、Desired-State 控制器 | `fabric_ledger.py`、`fabric_outbox.py`、`fabric_desired.py` | ✅ |

### F9 工程质量门禁（工程自身）
| 编号 | 需求点 | 证据 | 状态 |
|---|---|---|---|
| FR-9.1 | `make ci`：1200+ 回归 + 42 eval + 50 harness 自测；G1-G5 结构门禁（行数/复杂度/根策略/风格/质量） | `Makefile`、`checks/`、`engineering.yaml`、`docs/CHECKS_REGISTRY.md` | ✅ |
| FR-9.2 | 零依赖可移植：门禁/验证器/扫描器只用标准库，核心三件套复制或 wheel 安装均可用 | `docs/CHECKS_REGISTRY.md`、`tests/test_packaging.py` | ✅ |
| FR-9.3 | 门禁自身有单测（检查器坏了必须能被发现）、dogfooding（自家门禁发现 god-file 后拆分 cli_rounds/cli_batch/cli_residue） | CHECKS_REGISTRY H0 | ✅ |

## 3. 非功能需求

| 编号 | 需求 | 说明 |
|---|---|---|
| NFR-1 | Fail closed | 非法 YAML/JSON/负数/NaN、gate 无裁决、验证器超限、未知占位符、缺失文档一律显式失败，绝不降级静默通过 |
| NFR-2 | 有界内存 | 输出 8KiB 块排空、索引/状态/日志有限行缓冲、被拒摘录 64KiB、证据 64KiB、错误预算线，超长输入启动前拒绝 |
| NFR-3 | 并发与隔离 | 项目单实例锁、worktree 隔离、并行波形屏障、验证器并行上限 4、限速令牌桶跨进程共享 |
| NFR-4 | 7×24 韧性 | 重试/硬截止/熔断/停滞分诊/轮转日志/断点续跑/指纹复用 |
| NFR-5 | 安全信任边界 | agent 输出与编排 JSON 均视为不可信：失败签名拒绝、会话限制、路径防穿越、密钥用户级存储、历史 message 不作指令 |
| NFR-6 | 可移植性 | 纯 stdlib、无外部服务依赖、agent 可适配（argument_flags 抽象一个 argv 契约） |
| NFR-7 | 诚实性 | completion 报告禁止伪造、历史 PASS 仅标 `GATE_PASS_OBSERVED`、AI 产出须按证据标准核对后提升 |
| NFR-8 | 配置治理 | 分层配置（CLI > 项目 > 用户 > 默认）、`config explain/check` 只读核验、敏感键脱敏、版本 fail closed |

## 4. 覆盖度评估与候选方向（观察项，非承诺）

按功能点覆盖率看，F1-F9 全部落地，docs/feature-matrix.md 中历史需求
（expansion A-I、USAGE_FEEDBACK P1-P7、LESSONS D2/D4 等）状态均为 Complete。

**候选观察**（对照 docs/deferred-backlog.md 与代码现状，无触发条件不立项）：
1. 运营智能埋点机制（deferred，缺事件 schema/隐私边界/消费方定义）
2. 多 Agent 协作调度深化（deferred，缺可复现调度缺陷与公平性/吞吐指标）
3. 验证器并行度 4 为硬编码常量（`validation_batch.py`），可配置化属增量优化
4. TUI 与 headless 能力对齐度可定期回归（TUI turn 复用同一 prompt/session/
   memory/失败签名机制，但命令面板覆盖子命令集有上限）
5. fabric 默认关闭（`device_fabric.enabled: false`），可作为可选启用项推广验证

## 5. 结论

项目功能架构完整：**交互入口 → 执行引擎 → 流水编排 → 规则智能 → 产品思维 →
治理知识 → 自推进 → 设备织网 → 工程门禁** 九域闭环，且每项需求点都有
`make ci`/验证器/eval 可执行证据支撑。当前阶段的功能重心已从"新增能力"
转向"运营成熟度"（埋点、调度公平性、织网推广），符合 deferred backlog 的
克制原则。