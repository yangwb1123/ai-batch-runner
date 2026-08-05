# Open Source Readiness Spec — 开源项目要素清单（L3 + 开源意图时）

## 1. 判定

仅当需求含开源/产品/对外/发布信号（productization_level = L3）且明确
开源意图时加载本规范。无开源意图 → 不生成开源全套（克制）。

## 2. 目录规范

```
my-project/
├── docs/          # 用户文档 + 架构文档
├── examples/      # 可运行示例
├── packages/      # 多包（如适用）
├── apps/          # 应用（如适用）
├── scripts/       # 开发脚本
├── benchmark/     # 基准测试
├── design/        # 设计文档
├── .github/       # CI + Issue/PR 模板
├── docker/ helm/  # 容器化/部署
└── README.md LICENSE CHANGELOG.md CONTRIBUTING.md
```

## 3. 文档要素

| 文件 | 要求 |
|---|---|
| README | 项目简介、安装、快速开始、核心 API、示例链接、徽章（CI/覆盖率） |
| LICENSE | 明确许可证（MIT/Apache-2.0 等），与依赖兼容 |
| CHANGELOG | 按语义化版本记录（Unreleased/版本段） |
| CONTRIBUTING | 开发环境、提交规范、PR 流程、测试要求 |
| CODE_OF_CONDUCT | 社区行为准则 |
| SECURITY | 漏洞报告渠道与响应策略 |
| .github/ISSUE_TEMPLATE + PULL_REQUEST_TEMPLATE | 规范提交 |
| Roadmap | 规划与优先级（可选） |

## 4. 工程要素

- CI（GitHub Actions 等）：lint + typecheck + test + build + 覆盖率
- 语义化版本（SemVer）；版本发布流程（tag + release notes）
- Docker/Compose/Helm 示例部署
- Dependabot/Renovate 依赖更新；依赖漏洞扫描（npm audit/govulncheck 等）
- SBOM 生成；许可证兼容性检查
- 基准测试（benchmark/）与文档化性能基线

## 5. 克制

- 无开源意图不生成以上全套；示例/文档质量为"可被他人使用"级别即可
- 开源要素不改变核心业务代码结构（目录规范除外）
