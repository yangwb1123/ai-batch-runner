# aero-id 批次运行

aero-id 仓内**不再 vendored 运行器**（`pbatch/`、`pi-batch.py`、`pi-batch.yaml`、
`quality.py`、`.pi-batch-*.yaml`）。引擎、validator 注册表与本目录的配置都在
ai-batch-runner 仓内；aero-id 仓只保留业务代码与 `docs/` 里的历史产出。

## 目录内容（source of truth）

| 文件 | 用途 | 源自 |
|---|---|---|
| `campaign.yaml` | 项目级自动发现 → 方向 → 实现 | `aero-id/pi-batch-campaign.yaml` |
| `campaign-pipeline.yaml` | campaign 实现的流水线模板 | `aero-id/.pi-batch-campaign-pipeline.yaml` |
| `impl-pipeline.yaml` | 单方向实现流水线 | `aero-id/.pi-batch-impl-pipeline.yaml` |
| `run-campaign.sh` | 同步配置进目标仓并以目标仓为 cwd 执行 | 仿 `scripts/sverp/run-campaign.sh` |

`aero-id/.pi-batch-impl-pipeline-r6.yaml` **没有迁入**：它不是合法 YAML（第 3 行即解析
失败），且仓内无任何引用——是一份陈旧产物，随本次清理删除。

## 运行

```sh
bash ~/ai-batch-runner/projects/aero-id/run-campaign.sh --dry-run   # 只出计划
bash ~/ai-batch-runner/projects/aero-id/run-campaign.sh --jobs 4 --max-directions 2
```

目标仓默认 `$HOME/aero-id`，用 `AERO_ID_REPO` 覆盖。配置只写入目标仓 gitignored 的
`.pi-batch/aero-id/`，不改动任何受版本控制的文件。

## 仓内 driver 脚本

`aero-id/scripts/drive-pending-directions.sh` 走的是 **pipeline 模式**（每个 run 自带
`docs/auto/runs/<name>/pipeline.yaml`），不是 campaign 模式：

```sh
python3 "$AI_BATCH_RUNNER/pi-batch.py" --pipeline "$pipe" --reuse --reuse-legacy --retries 1
```

其中 `AI_BATCH_RUNNER` 默认 `~/ai-batch-runner`，cwd 仍是目标仓。pipeline 模板在仓内
（`docs/auto/runs/**`），因此无需同步——只有 campaign 模式强制要求配置位于仓内。

## validator 变化

原 `pi-batch.yaml` 的 `pyquality` validator 指向 `{cwd}/quality.py`（仓内 Python 代码
组织扫描器）。引擎改从本仓运行后该 validator 不可用，campaign/pipeline 不引用它；
需要时用本仓的 `python3 quality.py --strict <path>` 直接调用，或改用 `make check-*` 系列目标。
