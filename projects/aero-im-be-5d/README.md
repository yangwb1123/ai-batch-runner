# aero-im 后端（Rust）5 天 campaign

`ai-batch-runner` 驱动的**后端专属**维护轨道：目标仓 `/home/u1/aero-im`，只碰
`crates/` 与 `migrations/`，**绝对不碰 `web/`**。

## 硬边界（写进每个 stage 的 prompt，且由仓内门禁兜底）

| 边界 | 落地方式 |
|---|---|
| 不做前端 | `discovery.roots` 只有 `crates` + `migrations`；`exclude` 含 `web`；analysis/implement/design/acceptance prompt 显式禁止 `web/`、CSS、SolidJS/Vite、pnpm |
| 不跑 DB / Docker / migration / 外网 | implement prompt 明确禁止；PG 门控测试保持 `#[ignore]`；campaign 只跑 hermetic 门禁 |
| 不擅自启用 durable-v1 / account-summary 生产开关 | prompt 显式禁止；`docs/sprint/TODO.md` 的 staging/生产验收项不得编辑 |
| 不弱化既有门禁 | 门禁命令全部来自目标仓自己的 `Makefile` / `AGENTS.md §4.3` |
| 改动不进 main | 不使用 `--auto-merge`；每个方向在 `.pi-batch/aero-im-be-5d/worktrees/` 的隔离 worktree 内提交，等人审 |
| 失败的 worktree 一律保留 | `recovery.restore_on_failure: false`；supervisor 不做任何 reset/stash/checkout |

## 门禁（validator 注册表在 `pi-batch.yaml`，由目标仓声明）

| 名称 | 命令 | 覆盖 |
|---|---|---|
| `repo-gate` | `make ci` | `aero-cli check` + `gate all`：file-size / truth / web / deps / complexity / fmt / migrations 脚本 + native 检查 |
| `repo-tests` | `cargo test --workspace --lib` | 单元测试全绿（AGENTS §4.3 底线） |
| `repo-clippy` | `cargo clippy --workspace --all-targets` | 不新增警告（root `Cargo.toml` 已 ban `unsafe_code`） |

三条命令都用「落盘 + 只回显尾部 60 行 + 原样透传退出码」包装。**这不是放宽门禁**：
runner 对 validator 输出有 256KiB 硬上限，超限即判失败；而本仓 `cargo test
--workspace --lib` 有 669 个 `#[ignore]` 的 PG/Redis 门控用例，原始输出远超上限
（那是输出体积，不是测试失败）。退出码仍是唯一的门禁裁决，语义 fail-closed 不变。
实测：repo-gate 3.5KB / repo-tests 6.8KB / repo-clippy 3.5KB，全部 exit 0。

`clippy` 用**不带 `-D warnings`** 的形式：仓内已有存量 pedantic 债（AGENTS §4.2
明确「storage/server 的存量 pedantic 债非本批次别背」），`make lint` 的 `-D warnings`
会把存量债判红。要求写进 prompt：**不得新增**警告。

`preflight.require_repo_validator: true` + `required_repo_validators` 保证任何模型
调用之前先验基线；`--preflight-strict` 让基线红时直接 fail closed（rc=4）。

## 运行

```sh
# 一次性 dry run（只列模块，不写任何文件）
bash run-campaign.sh --dry-run

# 5 天无人值守（120h 墙钟 + 配额风暴看门狗）
setsid nohup bash supervise-5d.sh > supervisor.log 2>&1 < /dev/null &
cat /tmp/pbatch-aero-im-be-5d/supervisor.pid
tail -f /tmp/pbatch-aero-im-be-5d/supervisor.log
tail -f /tmp/pbatch-aero-im-be-5d/campaign.log
```

运行态（不写进目标仓）：

| 路径 | 内容 |
|---|---|
| `/tmp/pbatch-aero-im-be-5d/supervisor.log` | supervisor 决策日志（启动 / 重启 / 停止原因） |
| `/tmp/pbatch-aero-im-be-5d/campaign.log` | runner 全量日志（provider 熔断、preflight、round 进度） |
| `/tmp/pbatch-aero-im-be-5d/events.jsonl` | metering 事件（task_finish / task_fail / gate_reject / budget_cap / stall_stop） |
| `/tmp/pbatch-aero-im-be-5d/status` | 停止原因一行 |
| `/tmp/pbatch-aero-im-be-5d/runtime/` | campaign 状态、worktree 索引、provider 熔断状态 |
| `/home/u1/aero-im/docs/auto/` | 需求/设计/审查/门禁/实现/验收产出 + `state.jsonl` + `SUMMARY.md` |
| `/home/u1/aero-im/.pi-batch/aero-im-be-5d/` | 同步进去的 config + 隔离 worktree（gitignored） |

`CARGO_TARGET_DIR` 固定为 `/home/u1/aero-im/target`：隔离 worktree 各自编译会重复
占用 100GB+（磁盘只剩 ~950GB），共享 target 让 cargo 命中缓存；pipeline 并发为 1，
锁竞争可忽略。

## 停止条件（全部 fail closed）

1. **墙钟**：`AERO_BE_HOURS`（默认 120h）到点即终止。
2. **配额/鉴权风暴**：15 分钟窗口内 provider 失败 ≥8 次（`AERO_BE_STORM_*` 可调）立即停机。共享的免费 provider 被打爆时继续跑只会空转。
3. **campaign 自带熔断**：`limits.provider_breaker`（连续 4 次 provider 失败 → 指数退避，冷却超 900s 预算则任务以明确 reason 失败）。
4. **无进展**：`recovery.stop_after_stalled_rounds: 2` —— 连续 2 个 round 零进展即 rc=4 停止。
5. **预算**：`--daily-budget 120` / `--budget-max 1200`，到顶 rc=3 停止等 owner 决定。
6. **基线红**：`--preflight-strict` 在任何模型调用前跑仓内门禁，失败即 rc=4。

## 调参

```sh
AERO_BE_HOURS=24 AERO_BE_STORM_THRESHOLD=4 bash supervise-5d.sh   # 短跑 + 更敏感熔断
AERO_IM_REPO=/path/to/clone bash supervise-5d.sh                  # 换目标仓
```

模型固定 `--provider opencode --model space-bunny-free --thinking max`（与用户要求一致）；
`--rate-limit 0.2 --rate-burst 1` 让这条轨道在共享配额下礼貌排队，而不是把其他
并行轨道一起打爆。
