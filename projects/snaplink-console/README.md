# snaplink-console 批次运行

snaplink-console 仓内**不再 vendored 运行器**（`pbatch/` 41 个文件、`pi-batch.py`、
`pi-batch.yaml`、`quality.py`）。引擎与本目录的配置都在 ai-batch-runner 仓内，console 仓
只保留 Dart 源码、`examples/` 里的仓库级示例模板与 `docs/` 里的历史产出。

## 目录内容（source of truth）

| 文件 | 用途 | 源自 |
|---|---|---|
| `campaign.yaml` | B6 审计读路径 campaign | `snaplink-console/docs/campaigns/campaign-console-b6.yaml` |
| `campaign-advance.yaml` | B6 第二轮（client_id 收尾 + 模块隔离） | `snaplink-console/docs/campaigns/campaign-console-b6-advance.yaml` |
| `run-campaign.sh` | 同步配置进目标仓并以目标仓为 cwd 执行 | 仿 `scripts/sverp/run-campaign.sh` |

`examples/repository-campaign-pipeline.yaml` **留在 console 仓内**：两个 campaign 的
`implementation.pipeline` 都是仓内相对路径，而运行器仓也有一份同名模板。

## 运行

```sh
bash ~/ai-batch-runner/projects/snaplink-console/run-campaign.sh --dry-run
bash ~/ai-batch-runner/projects/snaplink-console/run-campaign.sh --jobs 4
bash ~/ai-batch-runner/projects/snaplink-console/run-campaign.sh --advance --dry-run
```

目标仓默认 `$HOME/snaplink-console`，用 `CONSOLE_REPO` 覆盖。

## 其它

- 仓内 `docs/campaigns/tasks-*.yaml` 本来就以 `~/ai-batch-runner/pi-batch.py` 调用，无需改动。
- `engineering.yaml` 只忽略运行态路径（`.pi-batch/worktrees/`、`docs/auto`），这两条保留；
  `root_policy.allowed_files` **本来就没有**放行运行器文件，所以删除不涉及白名单调整——
  也就是说，删除后根目录策略对这些文件本就没有开口子。
- `checks/directory_fanout.py` 与 `engineering.yaml` 里关于 `docs/auto` 的注释提到
  "pi-batch 运行时产物"，指的是运行态目录（保留），不是引擎位置，措辞仍然成立。
- 原 `pi-batch.yaml` 的 `pyquality` 指向仓内 `{cwd}/quality.py`（一个 Python 代码组织
  扫描器，在 Dart 仓里本就无调用方）。引擎改从运行器启动后该 validator 不可用；
  console 的门禁是 Dart 侧的 `python3 cli.py check`（filesize + analyze 等）。
