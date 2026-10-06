# 运行态目录的存量与保全

本文记录 `PBATCH_RUNTIME_HOME`（旧默认 `~/.codex/pbatch`，现默认 `~/.abr`）
在长期无人值守下会堆积什么、哪些可以安全丢弃、哪些不能——依据是本机一次
实测盘点，而不是推测。

盘点时间：2026-10-05。范围：`~/.codex/pbatch`，总量 **158 G**。

## 1. 存量高度集中

初次盘点：8117 个 workspace，总量 **158 G**。

| 类别 | 数量 | 体积 |
|---|---|---|
| 真实 campaign workspace（>100M） | 11 | **156.6 G** |
| 纯残留（只有 lock / breaker 状态） | 250 | 可忽略 |
| 含真实小数据（sessions.jsonl / campaign / memory） | 7867 | **0.53 G** |

250 个纯残留已删。**剩下这 7867 个不能按体积删**——它们占 0.53 G 却每一个
都有真实数据（2156 个含 `sessions.jsonl` 记忆索引，其余含 `campaign/`、
`memory/`、`evolution/` 台账），只是每个都很小。

结论：**按体积判断保留与否会完全判错**。0.53 G 的东西要删，156 G 里的
东西大部分不能删，两者体积差三个数量级而性质完全相反。

### 1.1 这 7867 个是怎么来的：测试向真实用户 runtime 泄漏

目录名给出了答案：`repo-sess-a` 到 `repo-sess-g` 各 125~133 个、
`repo-task-h` 125 个、`repo-task-copy-env` 133 个、`repo-task-scoped-memory`
133 个、`inputs-*` 1232 个——**参数化 pytest fixture 的典型形状**
（见 `tests/test_isolate.py`）。仅 2026-10-05 一天就新增 856 个。

对照实验（campaign 全部停止后）：

| 运行的测试 | 新增 workspace |
|---|---|
| `tests/test_runtime_paths.py` | 0 |
| `tests/test_gate_cli.py` + `test_task_center.py` | 0 |
| `tests/test_isolate.py` + `test_campaign.py` + `test_campaign_merge.py` + `test_task_center.py`（112 例） | 0 |

即：**本仓库自己的测试套件现在对 runtime 是 hermetic 的**（历史提交
`make-tests-hermetic-against-runtime-locks-and-da`、`hermeticize-test-runtime-state-and-lock-isolatio`
等确实生效了）。今天那 856 个来自 **campaign 驱动的 pytest**——campaign 会
在目标 worktree 里跑本仓库的测试套件，而那条路径没有把
`PBATCH_RUNTIME_HOME` 指向隔离目录，于是写进了真实用户 runtime。

这是仍然存在的缺口：**修复只覆盖了 `make test`，没覆盖「agent/campaign
在别人仓库里跑本仓库测试」这条路径。**

## 2. 大 workspace 里的 worktree 才是真问题

`aero-im-solid-24h` 一个目录占 135 G，几乎全部是 `worktrees/`——每个方向
一个目标仓库的完整 checkout。按「HEAD 是否已进入目标仓库 main」与
「工作区是否干净」两个维度分类：

| 状态 | worktree 数 | 成果 |
|---|---|---|
| 已合并且工作区干净 | 42 | 无（纯残留，已删） |
| **未合并的提交** | 26 | **25 个提交**：双因素认证、置顶消息、通知收件箱、文件与图片附件、音视频通话、房间轮询、消息搜索、未读回执、keyset 分页… |
| **已合并但有未提交改动** | 11 | **116 个文件**：Canvas 迁移 ×4、OIDC 登录 ×2、MFA、chatshell、登录策略 |
| 小计（已保全） | **37** | — |

**未合并≠无用。** 一个 campaign 失败、gATE 被拒或 operator 手动停机之后，
方向 worktree 就留在 runtime 里不动了；跑得越久、方向越多，这类残留越多。
它们是**别人仓库的成果**，既不能提交进本仓库（那正是「他项目数据污染本
仓库」），也不能因为「在 runtime 里」就当垃圾删掉。

## 3. 保全而非搬运

worktree 的 `.git` 是指向目标仓库 `.git/worktrees/` 的指针，**直接移动目录
会打断注册**，所以「迁移到新 runtime 根」对这些目录不成立。保全方式是把
工作导出成不依赖任何仓库的 patch：

```
<runtime>/preserved/<workspace>/<direction>.commits.patch    # git format-patch main..HEAD
<runtime>/preserved/<workspace>/<direction>.wip.patch        # git diff HEAD（未提交改动）
<runtime>/preserved/<workspace>/<direction>.untracked.tgz   # git ls-files --others
<runtime>/preserved/<workspace>/MANIFEST.md
```

实测导出 508 K（相对 135 G 压缩了 27 万倍），覆盖 25 个提交 + 11 份未提交
改动 + 10 个未跟踪文件包。

**patch 能保全，但不能直接应用。** 在目标仓库当前 main 上实测：26 个
commits patch **无一能干净 apply**，全部产生 content 冲突（集中在
`web/ws.test.js`、`web/test-solid/ChatShell.test.tsx`、`web/solid_api.test.js`、
`web/package.json`）。原因是分叉点之后的 main 已被后续合并改动过。
这是**正常的分叉**，不是 patch 损坏——patch 本身完整（抽查一个含 13 个文件、
1607 行真实改动）。恢复到目标仓库需要 rebase/人工整合，patch 的作用是
「保证工作不会消失」，不是「保证能一键恢复」。

## 4. 由此得出的保留判据

按证据而非体积决定去留：

| 判据 | 动作 |
|---|---|
| 仅含 lock / breaker 等无状态残留 | 可删，无信息损失 |
| worktree HEAD 已是目标仓库 main 的祖先，且工作区干净 | 可删，成果已在 main |
| worktree 有独有提交 | 先 `format-patch` 保全，再按目标仓库正常评审合并 |
| worktree 工作区有改动 | 先导出 `diff HEAD` 与未跟踪文件，再谈处置 |

任何一条**没有对应动作就删除**的，都是在销毁未合并成果。

## 5. 本次实际处置

| 动作 | 结果 |
|---|---|
| 保全 37 个方向 worktree 到 `~/.abr/preserved/aero-im-solid-24h/` | 25 提交 + 11 份未提交 + 10 个未跟踪包，共 508 K |
| 删除 42 个已合并且干净的 worktree | 释放 24 G |
| 删除 11362 个 `test_*` 污染 workspace | — |
| 删除 250 个纯 lock/breaker 残留 | 可忽略体积 |

未删除：135 G 的 37 个保全源目录（保全只保证不丢，不替代正常评审合并）、
7867 个含真实小数据的 workspace。

## 6. 与本仓库的边界

本文只记录**本工具的运行态目录会如何堆积**这一运维事实，不引入任何他项目
的代码或产物。保全出来的 patch 留在 `PBATCH_RUNTIME_HOME/preserved/`
（默认 `~/.abr/preserved/`），属于目标仓库的成果，不进版本库。
