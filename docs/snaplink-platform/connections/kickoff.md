文档已产出：`docs/snaplink-platform/connections/kickoff.md`（149 行 ≥ 80 行要求；32 处 Verified、17 处 [PROPOSED] 标注；无自指空壳标记）。

**产出摘要**：

- **A. 连接矩阵（8 条）**：逐条列出发起方/接收方/接口/凭据/现状/合规性初判/证据等级——6 条已对接（1 sverpweb→snaplink OIDC+SDK、2 sverpweb→governance outbox L2、3 sverpweb→vault presign、4 sverpweb→im 广播、5 console→snaplink 直连、6 vault→snaplink JWKS）、1 条部分（7，aero-id gRPC 未定案）、1 条未落地（8，两 token BFF）。额外标注合规性：连接 5 命中禁止项 `console-browser-bypasses-service-api`。
- **B. 信任边界**：snaplink 唯一签发方 → vault/im/id/governance 验签方；信任图 + 三个服务账号最小权限逐条对照 scope-matrix 六条横切 deny 规则（sso-admin 三域与通配符存疑、sverp-audit 读侧无专用账号、sverp-vault admin 使用核查、im app token 未入注册表）；decision_id 契约桥列为阶段 3 必查项。
- **C. 缺口清单**：C-1…C-8 逐条连接 + C-9…C-12 跨切（snaplink/aero-im 无 connector、scope 注册表、委托模型、schema 审批链），每条带归属项目。
- **D. 分析框架**：7 项目 × 6 维度（入站/出站/凭据/审计/通知/存储）矩阵，每格给出该维度在阶段 3 的具体核查或处置内容；前置三层判断门 + 版本纪律。
- **E/F**：12 个开放决策（scope-matrix 6 + integration-design 6）归属一览；证据边界说明。

按流水线定义，本文件即 kickoff 阶段产物，将作为阶段 3 七个分项目任务的输入（`from_outputs: kickoff`）。
