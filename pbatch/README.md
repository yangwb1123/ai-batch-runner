# pbatch — pi-batch 核心包

> 知识维护（maintenance-intelligence 06）：模块现状 + 决策记录，新人可接手。

## 职责

| 模块 | 职责 |
|---|---|
| `cli.py` | 命令行入口：参数解析、main、轮循环、单批/流水线分发 |
| `config.py` | 声明式配置（pi-batch.yaml）、agent 默认值、会话标志、验证器注册表 |
| `config_inspect.py` / `config_cli.py` | 配置来源 provenance、版本/结构校验、脱敏 explain/check 报告 |
| `models.py` | 数据模型：Task / TaskResult / Stage / Pipeline |
| `runner.py` | 任务执行：硬超时、失败签名拒绝、重试、验证门禁、摘要 |
| `pipeline.py` | 流水线：阶段构建、meta 角色编排、gate 裁决、决策日志、归档 |
| `gate_contract.py` | Gate 结构化裁决契约、证据/验证器校验与旧 VERDICT 协议兼容 |
| `pipeline_schema.py` / `evidence.py` | fail-closed schema 与有界证据注入 |
| `relevance.py` | meta 角色关键词评分、命中证据、去重与数量上限 |
| `classifier.py` | 任务类型判定（前端→UI 流水线/后端→后端流水线）+ **system_type 系统分类学** |
| `rule_matcher.py` | 规则匹配：scale × page_type × risk 三轴 + LLM 双向校验 |
| `advance.py` | 自推进迭代引擎（扫描→分批→state.jsonl 续跑） |
| `assessor.py` | 需求评估：8 维检查、规模处方、产品化分级、工作流风险分级 |
| `product.py` | 产品思维层：productization_level、场景推演链、商业/开源要素 |
| `learn.py` | 自演进闭环：事故/RCA → Rule Schema 草案 → 人工批准并入注册表 |
| `transaction.py` | 决策事务契约：guard/预算前置门、预期证据验证、补偿回滚与 JSON 重放 |
| `world.py` | 世界状态平面：fsync WAL、原子快照、单调世界版本、计划绑定、对账与漂移裁决 |
| `capabilities.py` | 能力注册与 CapabilityGrant：信任等级、主体/节点/设备/任务/资源/环境/数据级别/有效期/副作用授权门 |
| `fabric.py` / `fabric_ledger.py` / `fabric_lifecycle.py` / `fabric_outbox.py` / `fabric_task_store.py` / `fabric_checkpoint_store.py` | ExecutionEvidence digest 绑定、设备/任务/尝试维度资源账本、幂等归账与 `/usage` 查询；设备生命周期合法转移、恢复日志与 `/lifecycle` 查询；Runner 证据离线缓冲/重放；控制面任务与 Checkpoint 快照、`/task/start` fencing 握手、主体授权与重启恢复 |
| `ratelimit.py` | F 线 provider 令牌桶限速 + jitter 重试 |
| `context.py` | 上下文路由：glob + 关键词加载项目文档 |
| `eval.py` | 规则系统回归套件 |
| `selfcheck.py` | `pi-batch check` 自检（quality + schema + eval） |
| `memory*.py` / `index_io.py` | 渐进式 message memory：有界索引、按需 recent/find/read |
| `campaign*.py` | 项目级模块发现、方向选择、worktree 隔离流水线 |
| `registry.py` | 规则注册表（learn 产物并入） |
| `retro.py` | 复盘记录 |
| `pipeline_status.py` | **结果状态常量集**（PASSED/GATE_REJECTED/VALIDATION_FAILED/PIPELINE_FAILED）——集中定义防拼写漂移 |

## 设计决策（ADR 摘要）

- **fail-closed**：坏 YAML/JSON/负数/NaN 在创建日志或子进程前以退出码 2 拒绝
- **失败零产物**：拒绝/验证失败的结果不留文件；被拒产物保留有界摘录到 `.pi-batch/rejected/`
- **证据有界注入**：`evidence.max_bytes`（64KiB）+ `max_sources`（64）
- **门禁守护**：`make ci`（502 测试）+ `quality.py --strict` + 3 个检查器自扫描
- **检查器适用域**：designintelligence/backendexperience/knowledge 面向业务服务代码；
  CLI 工具本体（本包）用 quality + 测试门禁守护（backendexperience 的
  long-op/audit 规则对同步 CLI 工具不适用）

## 常用命令

```bash
python -m pbatch --help          # 入口（pip install . 后 pi-batch 同）
python -m pbatch check           # 自检
python -m pbatch eval            # 回归
python -m pbatch classify "订单审批流" --json   # 含 system_type
python -m pbatch transaction --input transaction.json --action validate --json
python -m pbatch world reconcile --json
python -m pbatch world status --json
python -m pbatch capabilities authorize --grant grant.json --capability world --json
python -m pbatch devices schedule --data-class internal --device NODE \
  --network-budget-bytes 1048576 -- CMD ARG...
python -m pbatch devices lifecycle --device NODE --json
# 已认证设备的证书指纹绑定轮换（证书由外部 CA 签发）
python -m pbatch devices rotate-certificate --device NODE --fingerprint SHA256
# 控制平面 mTLS：serve 与 runner 两端均提供 CA/证书/私钥
python -m pbatch serve --cert-file server.crt --key-file server.key --ca-file ca.crt --require-client-cert
# 控制面配额：租户并发 + 设备并发；配额在任务准入时预留，终态时释放
python -m pbatch serve --tenant-quota team-a=2 \
  --device-quota node-a=1,rn-b=2
# Gang：控制面预检并在任务锁内原子准入，任一目标不可用则整批拒绝
python -m pbatch devices schedule --gang --devices node-a,node-b \
  --as-device node-a -- echo gang
```
