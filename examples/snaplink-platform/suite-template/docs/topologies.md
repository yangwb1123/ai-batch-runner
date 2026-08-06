# 部署拓扑选择指南

| 拓扑 | compose | 组件 | 适用 |
|---|---|---|---|
| minimal | compose/minimal.yaml | snaplink | 普通后台系统、API 鉴权、SSO、第三方应用接入 |
| identity | compose/identity.yaml | snaplink + aero-id + console | 需要账户/组织管理 |
| vault | compose/vault.yaml | 任意 OIDC Provider + aero-vault | 只用文件服务的产品（标准协议组合） |
| full | compose/full.yaml | 6 服务 + 集成依赖 | 完整企业组合（审计 L2、通知 aero-im） |

## 选择规则

1. **不强制整体部署**：full 只是其中一种拓扑。按客户需求裁剪。
2. **标准协议优先**：aero-vault 支持任意标准 OIDC Provider，Snaplink 提供
   增强模式（细粒度权限、租户上下文、decision_id）。
3. **生产分布式部署**：各服务独立集群/独立数据库/独立版本/独立回滚，
   见各产品仓库 DEPLOY.md。
4. **本地一体化开发**：full.yaml 一条命令拉起，仅用于开发验证。

## 拓扑变更流程（Agent 规则）

1. 在 compatibility.yaml 增加/修改拓扑
2. compose 文件与拓扑 components 保持一致（suite-check.py 门禁）
3. 冒烟通过后更新本指南与 README
4. AI 产出的拓扑变更 = 提案，须人工批准
