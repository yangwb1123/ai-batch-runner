# T14 · 难度感知模型路由 + 预算感知降级

> 7×24 无人值守的成本与质量平衡器：按任务难度选 model，按每日 token 配额自动降档。

## 用法

```bash
# 规划预览（零 LLM 成本，确定性画像）
pi-batch route "修复生产环境的偶发空指针崩溃，涉及支付链路，需要尽快上线"
# tier     low  (DOWNGRADED)
# model    deepseek-v4-flash
# thinking max
# quota    10% remaining today
#   - profile mode=3 workflow=L3_platform
#   - daily quota remaining 10% < 25%: downgraded to 'low'

pi-batch route "查一下这个配置怎么改" --json        # 结构化输出

# 自动注入：任务跑之前按难度选 model（仅补未显式指定的任务）
pi-batch -p "..." --route-model
```

## 配置（pi-batch.yaml `model_routing`，默认关）

```yaml
model_routing:
  enabled: false                       # 显式开启
  tiers:
    low:      {model: "deepseek-v4-flash"}
    medium:   {model: "gpt-5.6-luna"}
    high:     {model: "gpt-5.6"}
    critical: {model: "gpt-5.6-sol"}   # 支付/生产事故/高风险
  downgrade:
    enabled: true
    remaining_ratio_threshold: 0.25    # 今日配额剩余 < 25% → 降档
    min_tier: low                      # 阈值触发后的目标档位
```

## 难度档判定（确定性启发式，复用 profile 12 维向量）

| 档 | 触发 |
|---|---|
| low | L1 / load < 0.6 |
| medium | L2 或 load ≥ 0.6 |
| high | L3 平台级 或 (risk=high + workload≥2) |
| critical | L3 且 hard-trigger（支付/生产/上线…，mode≥3） |

## 纪律

- **默认关**：未配置仓库零影响；`route` 命令本身始终可作规划预览
- **只补缺省**：`--model` / 任务 / 阶段显式指定的 model 永不覆盖（单测锚定）
- **可解释**：RoutingPlan 带 reasons，`--json` 输出降级原因
- **成本控制非安全边界**：与 metering daily budget 同款语义（fail-open 读计数）

## 与 7×24 组合运营（完整配方）

```bash
pi-batch tasks.yaml --max-rounds 0 --round-delay 300 --min-interval 5 \
  --retries 3 --reuse --wait-lock 60 \
  --run-domain nightly-batch --route-model \
  --daily-budget 200 --rate-limit 1 --rate-burst 4 \
  --log-file logs/247.log --webhook http://ops.internal/hook
```

链路：`--max-rounds 0` 无限轮循环（7×24）· `--run-domain` 与其他实例并行 ·
`--route-model` 每轮按难度/额度选 model · `--daily-budget` 每日额度封顶 →
剩余 <25% 自动降档 · `--rate-limit` 节流 · `--reuse`/`--retries` 断点续跑 ·
`--wait-lock` 锁排队。无人值守且成本可控。
