# aero-im 批次运行

aero-im 仓内**不再 vendored 运行器**（`pbatch/`、`pi-batch.py`、`pi-batch.yaml`、
`quality.py`）。引擎与本目录的配置都在 ai-batch-runner 仓内，aero-im 仓只保留业务代码与
`docs/` 里的历史产出。

## 目录内容（source of truth）

| 文件 | 用途 | 源自 |
|---|---|---|
| `campaign.yaml` | B5 批次 campaign（发现 → 方向 → 实现） | `aero-im/docs/campaigns/campaign-aero-im-b5.yaml` |
| `run-campaign.sh` | 同步配置进目标仓并以目标仓为 cwd 执行 | 仿 `scripts/sverp/run-campaign.sh` |

`examples/repository-campaign-pipeline.yaml` **留在 aero-im 仓内**：它是仓库级示例模板
（运行器仓也有一份），而 campaign 的 `implementation.pipeline` 是仓内相对路径。

## 运行

```sh
bash ~/ai-batch-runner/projects/aero-im/run-campaign.sh --dry-run
bash ~/ai-batch-runner/projects/aero-im/run-campaign.sh --jobs 4 --max-directions 2
```

目标仓默认 `$HOME/aero-im`，用 `AERO_IM_REPO` 覆盖。

## 仓内 driver 脚本

`aero-im/scripts/resume-b54-acceptance.sh` 走 **pipeline 模式**（每个 run 自带
`docs/auto/runs/<name>/pipeline.yaml`），模板在仓内，无需同步；它此前把运行器路径写死在
`/home/u1/ai-batch-runner/pi-batch.py`（本机不存在该路径），现改为
`${AI_BATCH_RUNNER:-$HOME/ai-batch-runner}/pi-batch.py`。

## validator 变化

原 `pi-batch.yaml` 的 `pyquality` 指向仓内 `{cwd}/quality.py`。引擎改从运行器仓启动后该
validator 不可用，campaign 不引用它；aero-im 的门禁是 `make ci`（cargo check + gates），
与运行器无关。
