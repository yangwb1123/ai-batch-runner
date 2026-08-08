文档已产出：`docs/snaplink-platform/connections/sverpweb.md`（143 行 ≥ 60 行要求；7 处 Verified、1 处 [PROPOSED] 标注）。**至此阶段 3 七个分项目文档全部完成。**

**A. 四连接清单**（代码级 Verified）：
1. **→ snaplink**：`SnaplinkTokenClient`（client_credentials，exp 缓存+提前刷新+401 强制重试）为所有服务客户端的认证基础件；三个服务账号（sso-admin/sverp-audit/sverp-vault）
2. **→ governance**：SQLite outbox（event_id 幂等+毒丸隔离）→ `governance-transport` 契约映射（schema_id=`sverp.{eventType}` 每事件一条、anonymous 显式标记、http.request.* 噪音跳过）→ `events:batch` 逐事件 receipt；338 事件全 published
3. **→ vault**：presign 直传（大文件）+ BFF 代理 PUT（小文件）+ **受控引用 `/api/resources/:key` 双重校验**（本地 canRead 可见性 + vault 服务端 ACL/owner）+ 部门同步（**仅启动时全量一次**）+ 本地 file_shares 投影
4. **→ im**：`/api/auth/login` email/password 应用登录 + ensureRoom("SVERP Integration") + sendMessage 房间广播，best_effort 通知

**B. 四项改动**（各含理由/影响面/验收标准）：
1. **B-1 定向通知**：维持广播（deferred 带触发条件）——切换前提是 subject→participant 映射表缺失，且应用账号身份下审计归因需配套
2. **B-2 直链下载**：**发现缺口**——presign get 签发仅 `authorized` 中间件、未做 canRead 资源级检查（与受控引用双重校验不对称）；share-link 维持 deferred
3. **B-3 审计专用账号**：**发现权限放大**——现状 scope=`audit:event:read audit:platform:cross_tenant`（platform 跨租户豁免）+ 代码硬编码 tenantId——应换 `sverp-audit-reader` 租户限定账号（与 audit-governance.md C-1 联动）
4. **B-4 双写风险核查**：三个风险点——① 撤销/删除不级联 vault ACL（残留授权）；② 部门同步仅启动一次（运行中组织变更无界滞留窗口）；③ BFF 代理上传无 key 级授权检查——四路径授权语义表成文 + 回归测试

**全流水线产出**：7 个分项目文档（snaplink/aero-id/aero-vault/aero-im/audit-governance/snaplink-console/sverpweb）已就位，跨文档结论一致（gRPC 校验建议 JWKS、审计写读分离、schema 审批链、share-link deferred、双 token BFF 前置链），可进入阶段 4 对抗审查。
