# aero-vault 批次运行

aero-vault 仓内**不再 vendored 运行器**（`pbatch/`、`pi-batch.py`、`pi-batch.yaml`、
`quality.py`、`pi-batch-campaign.yaml`、`pi-batch-campaign-pipeline.yaml`）。引擎与本目录的
配置都在 ai-batch-runner 仓内，aero-vault 仓只保留业务代码与 `docs/` 里的历史产出。

> 注意：仓内 `ai-dev/` 是**另一个自洽的 AI 工作区**（19 个 `ai/` 配置文件、18 个 prompts、
> 3 个 pipelines，以及自己的 `pi-batch.py` / `pi-batch.yaml`）。`ai-dev/ai/run-review.py`
> 读取 `ai-dev/pi-batch.yaml`，`ai-dev/ai/README.md` 记录了它的调用方式，因此本次**不动**
> `ai-dev/`。它与根目录那份陈旧分叉不同类，去留是另一个决定。

## 目录内容（source of truth）

| 文件 | 用途 | 源自 |
|---|---|---|
| `campaign.yaml` | 项目级维护 campaign（发现 → 方向 → 实现） | `aero-vault/pi-batch-campaign.yaml` |
| `campaign-pipeline.yaml` | 实现流水线模板 | `aero-vault/pi-batch-campaign-pipeline.yaml` |
| `run-campaign.sh` | 同步配置进目标仓并以目标仓为 cwd 执行 | 仿 `scripts/sverp/run-campaign.sh` |

`campaign.yaml` 的 `implementation.pipeline` 已改为仓内相对路径
`.pi-batch/aero-vault/campaign-pipeline.yaml`，与 `run-campaign.sh` 的同步目标一致。

## 运行

```sh
bash ~/ai-batch-runner/projects/aero-vault/run-campaign.sh --dry-run
bash ~/ai-batch-runner/projects/aero-vault/run-campaign.sh --top-only --reuse --skip-passed --retry-failed
```

目标仓默认 `$HOME/aero-vault`，用 `AERO_VAULT_REPO` 覆盖。

## 门禁

`engineering.yaml` 的 `root_policy.allowed_files` 原本显式放行 5 个运行器文件
（`pi-batch.py`、`pi-batch.yaml`、`quality.py`、`pi-batch-campaign.yaml`、
`pi-batch-campaign-pipeline.yaml`），已一并删除：白名单不再为 vendored 工具开口子，
根目录出现同类文件会直接被 `python3 cli.py check` 拦下。

## validator 变化

原 `pi-batch.yaml` 的 `pyquality` 指向仓内 `{cwd}/quality.py`。引擎改从运行器仓启动后该
validator 不可用，campaign 不引用它；aero-vault 的门禁是 `python3 cli.py check` /
`harness`（Go 侧），与运行器无关。
