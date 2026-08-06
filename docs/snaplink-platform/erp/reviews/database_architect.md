审查完成，交付物已写入 `docs/snaplink-platform/erp/reviews2/database_architect.md`。

## 核心结论摘要

**本阶段实际执行**：19 个存储层测试（全部绿）、tsc 复现 withActor 三连错误、生产库 sqlite_master 实况核对、legacy 媒体行 404 运行时复现、governance 源码（ValidateBasic/409 三分支/文件后端）与 `go test ./...` 全绿核实。

### 1. Store 清单（10 项，全部接线状态实测）
- **源码/本地 dist 中所有 store 均默认接线 SQLite 落盘**（app.ts 默认值），无"SPI 在树但未接线"；但**生产库与源码 schema 漂移**：无 `audit_outbox` 表、media 无 `storage_key` 列 → 线上部署的是 audit 改造前旧版二进制（"stock 二进制接线"在生产上不成立）。
- **hot vs durable**：除 auth 开发默认 memory（生产 .env 已设 sqlite）外全部持久；全部 `synchronous=NORMAL`；governance 文件后端整快照驻内存、每写全量重写 JSON。

### 2. 主要发现（DB 面新增 4 项，其余支持 revision）
- **F-D1 [High]** 升级 = 隐式、无版本、无备份的就地迁移（CREATE audit_outbox + ALTER media）→ 给出升级预检序列
- **F-D2 [High]** 遗留媒体行（data:/uploads，生产实库 2 行）在新代码下内容 404——**运行时复现**；M3 迁移范围遗漏，且回填无源可读（质量侧有 legacy 兼容、媒体侧没有）
- **F-D3 [High]** 无限重试无终态（支持 revision §2.2）+ **补充缺口**：published 行无保留策略/purge 端点
- **F-D4 [High]** B2 的 DB 面实锤：actor 恒 null + 顶层键污染 → governance `actor.id` 必填 → 回退事件 L2 下必然全批 400
- **F-D8 [Medium]** `synchronous=NORMAL` 断电窗口 + 无一致性备份/演练 → 给出 `sqlite3 .backup` 方案
- **F-D11 [Medium]** governance 幂等键检查 O(N) 全扫 → owner #8 硬化项补"幂等键唯一索引"

### 3. 事务/索引分析
outbox claim CAS（lock_token + 租约回收）设计正确；质检 IMMEDIATE 事务 + 归属校验 + 共享附件所有权转移正确；APS/BS 乐观版本 CAS 正确；认证 `DELETE RETURNING` 原子单次消费正确。缺口：双投递依赖对端 202 去重（需测试钉死）、`LIKE '%..%'` 索引失效、media 全表扫描无分页。

### 4. 迁移序列
三面：二进制升级（备份→完整性基线→部署→4 条校验 SQL→回退验证）、M3 双读+回填+按批对账、L2 五步 runbook（可逆回退无损）。

### 5. 未知量
业务量基准、保留期值（owner #5）、RTO/RPO（#8）、governance 硬化基准、schemaVersion/occurred_at 兼容值、vault CORS/TTL、线上二进制漂移实机幅度、API 实例数规划。

本评审为咨询性意见，不构成发布批准；不豁免任何仓库门禁。
