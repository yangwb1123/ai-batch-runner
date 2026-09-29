# snaplink（IdP）批次运行

snaplink 仓内**不再 vendored 运行器**（根目录 `pbatch/`、`pi-batch.py`、`pi-batch.yaml`、
`quality.py`，以及 `ai-dev/` 下的同款副本，见下）。引擎与本目录的配置都在 ai-batch-runner
仓内，snaplink 仓只保留业务代码、`ai-dev/` 里的项目自有材料与 `docs/` 里的历史产出。

## 目录内容（source of truth）

| 文件 | 用途 | 源自 |
|---|---|---|
| `campaign.yaml` | B4 批次 campaign（claims / scope registry） | `snaplink/docs/campaigns/campaign-snaplink-b4.yaml` |
| `campaign-pipeline.yaml` | 实现流水线模板 | `snaplink/docs/campaigns/repository-campaign-pipeline.yaml` |
| `pi-batch.yaml` | 项目 validator 注册表（`cli.py check`、`gofmt`、`build`、`config`、`root`） | `snaplink/ai-dev/pi-batch.yaml` |
| `run-campaign.sh` | 同步配置进目标仓并以目标仓为 cwd 执行 | 仿 `scripts/sverp/run-campaign.sh` |

`campaign.yaml` 的 `implementation.pipeline` 改为仓内相对路径
`.pi-batch/snaplink/campaign-pipeline.yaml`，与 `run-campaign.sh` 的同步目标一致。

## 运行

```sh
bash ~/ai-batch-runner/projects/snaplink/run-campaign.sh --dry-run
bash ~/ai-batch-runner/projects/snaplink/run-campaign.sh --jobs 4 --max-directions 2
```

目标仓默认 `$HOME/snaplink`，用 `SNAPLINK_REPO` 覆盖。

## `ai-dev/` 的处理

`ai-dev/` 里原本是整套工具集的**上游陈旧快照**（`ai/`、`prompts/`、`pipelines/` 相对本仓
上游只差路径改写；`ai-dev/ai/run-review.py` 731 行 vs 上游 912 行；`ai-dev/git-auto-commit.sh`
3442 字节 vs 上游 4494 字节），外加自己的 `ai-dev/pbatch/` 与 `ai-dev/pi-batch.py` 引擎副本。
这些已从 snaplink 删除，改用本仓的 `ai/`、`prompts/`、`pipelines/`、`scripts/`。

**保留**（snaplink 自有，不是上游副本）：`ai-dev/AGENTS.md`、`ai-dev/docs/**`、
`ai-dev/examples/**`（含 `quickstart-snaplink-analysis.yaml`、`snaplink-proposals.yaml` 等
snaplink 专属输入）。

## validator 变化

项目 validator 注册表搬到了 `pi-batch.yaml`（本目录），其中 `pyquality` 原本指向仓内
`{cwd}/ai-dev/quality.py`；引擎从本仓启动后该路径不可用，改注为使用运行器自带的
`quality.py`。snaplink 自身的门禁是 `python cli.py check` / `make ci`。
