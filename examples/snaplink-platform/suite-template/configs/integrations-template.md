# 各服务组合配置模板（integrations 段落）

原则：**配置发现而不是硬编码服务关系**。每个产品仓库的配置应包含
可选的 integrations 段落；未启用某集成时使用本地/降级实现（L0/L1）。

## aero-vault（示例）

```yaml
integrations:
  authorization:
    enabled: true
    provider: snaplink          # 或 local-jwt / 任意标准 OIDC Provider
    issuer: https://auth.example.com
    audience: aero-vault
    decision_endpoint: https://auth.example.com/v1/authorization/check

  audit:
    enabled: true
    provider: snaplink-audit-governance   # 或 local-log / webhook / message-bus
    transport: nats
    topic: audit.events.v1

  notification:
    enabled: true
    provider: aero-im            # 或 smtp / noop
    transport: http
    base_url: http://aero-im:8080

  storage:
    provider: s3                  # AWS S3 / MinIO / OSS / COS
    endpoint: http://minio:9000
    bucket: vault
```

降级矩阵（必须定义）：

| 集成 | 可用 | 不可用（降级） |
|---|---|---|
| authorization | 细粒度决策 + decision_id 审计 | 拒绝高风险操作，允许只读（或 fail-closed 按产品策略） |
| audit | L2 governance 全量事件 | L1 本地结构化日志缓冲，恢复后重放（Outbox） |
| notification | aero-im 送达 | 本地队列/跳过通知，不影响主流程 |
| storage | 全部 | 不可用（核心依赖，健康检查告警） |

## 启动校验清单

- 配置必填项检查（启动即失败，不静默降级到错误配置）
- 配置版本号；Secret 引用（不从明文配置文件读密钥）
- 健康状态端点暴露各集成连通性
- 超时/重试/Circuit Breaker 配置化
