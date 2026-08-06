# sverpweb-stable 路由迁移实施清单（route-migration backlog）

> 依据 `sverpweb-stable/docs/route-migration.md`（状态真值）与参考项目
> `/home/dwp/workspace/job/sverpweb`（业务规格）。每组用
> `route-migration-pipeline.yaml` 驱动，跑完一组验证一组。
>
> 运行（在 sverpweb-stable 仓库根）：
> ```bash
> python /home/dwp/ai-batch-runner/pi-batch.py \
>   /home/dwp/ai-batch-runner/projects/sverp/route-migration-pipeline.yaml \
>   -p "<Gx 描述>" --retries 2 --log-file logs/route-gx.log
> ```

## 完成进度

- [x] G1 erp/bs/ship — /order-ship-tracing（2025-08 完成）
- [ ] G2 erp/pchs 组 — /purchase/prod-schedule + /purchase/prod-outsourcing
- [ ] G3 erp/wh 组 — /warehouse/{purchase-receipt, material-supply, leather-preparation-board, stock, shipment}
- [ ] G4 erp/qaqc 组 — /qa-qc/production-inspection{,/new,/:id}
- [ ] G5 erp/prod/model — /prod/computer-car-model-manage
- [ ] G6 srm/sharing — /srm/pchs/process-sharing
- [ ] G7 /m/mold/:itemCode — 公开移动端模具详情

## 组描述（-p 参数，可直接复制）

### G2 — erp/pchs（父路由 + 2 叶）
```
G2: erp/pchs 组 — 父路由 /pchs + erp/pchs/schedule（/purchase/prod-schedule，
钢板计划与采购进度）+ erp/pchs/outsource（/purchase/prod-outsourcing，委外
加工）。参考业务规格：
- /home/dwp/workspace/job/sverpweb/apps/web/src/pages/PCHS/ProdSchd/（index 主
  入口 + kh/so/po/pq 四个页签组件 + helpers）+ apps/web/src/pages/PCHS/Outsourcing/
- API 客户端 /home/dwp/workspace/job/sverpweb/apps/web/src/apis/pchs.ts
  （getPlan/getPO/getPQ/getPurchaseProcess 等）
- /home/dwp/workspace/job/sverpweb/apps/api/src/modules/pchs/router.ts
参考 seed key：erp/pchs、erp/pchs/schedule、erp/pchs/outsource。权限码建议
erp:pchs:read / erp:pchs:schedule:read / erp:pchs:outsource:read（写操作
erp:pchs:schedule:write 等）。MSSQL 仅为参考，stable 用 SQLite+演示数据。
```

### G3 — erp/wh（父路由 + 5 叶）
```
G3: erp/wh 组 — 父路由 /wh + 5 个叶路由：
- erp/wh/receipt（/warehouse/purchase-receipt，采购入库）
- erp/wh/material（/warehouse/material-supply，配料发料，含 finishedSupply/
  phaseTable/statistics 子视图）
- erp/wh/leather（/warehouse/leather-preparation-board，皮革备料看板）
- erp/wh/stock（/warehouse/stock，库存，含 Chart 图表）
- erp/wh/shipment（/warehouse/shipment，发货）
参考业务规格：/home/dwp/workspace/job/sverpweb/apps/web/src/pages/WH/ 各子目录
（PurchaseReceipt/MaterialSupply/LeatherPreparation/Stock/Shipment，ant-design-vue
+ echarts 图表）+ /home/dwp/workspace/job/sverpweb/apps/api/src/modules/wh/（router.ts
+ receipt.ts）。参考 seed key：erp/wh、erp/wh/receipt、erp/wh/material、
erp/wh/leather、erp/wh/stock、erp/wh/shipment。权限码建议 erp:wh:read 与
各叶 erp:wh:<leaf>:read/write。MSSQL 仅为参考，stable 用 SQLite+演示数据；
图表用前端可用的轻量方案（参考 stable 现有实现，勿引入新重依赖）。
```

### G4 — erp/qaqc（父路由 + 3 叶）
```
G4: erp/qaqc 组 — 父路由 /qa-qc + erp/qaqc/inspection
（/qa-qc/production-inspection，生产检验列表）+ /new（新建，含子表/模板/
文件上传）+ /:id（详情，含明细编辑与审批签名）。参考业务规格：
- /home/dwp/workspace/job/sverpweb/apps/web/src/pages/Quality/ProductionInspection/
  （index/DetailPage + components：OverviewTable/Subtable/DetailForm/
  TemplateManagement，ant-design-vue）
- /home/dwp/workspace/job/sverpweb/apps/api/src/modules/qa/（router.ts 的
  prodCheck/prodCheckItem/prodCheckItemTemplate/prodCheckFile 端点 +
  patrol.ts + bom.ts + knowledge.ts）
- API 客户端 /home/dwp/workspace/job/sverpweb/apps/web/src/apis/qa.ts
参考 seed key：erp/qaqc、erp/qaqc/inspection、erp/qaqc/inspection/new（hidden）、
erp/qaqc/inspection/detail（hidden，:id 参数）。权限码建议
erp:qaqc:inspection:read/write。数据层 SQLite+演示数据；BOM/知识库为参考
高级能力，可按 route-migration.md 的克制原则以 [~] 交付（核心 CRUD+明细+
文件上传必须完整）。
```

### G5 — erp/prod/model（父路由 + 1 叶）
```
G5: erp/prod/model — /prod/computer-car-model-manage（电脑车型号管理，
亚克力模具台账）。参考业务规格：
- /home/dwp/workspace/job/sverpweb/apps/web/src/pages/PROD/ComputerCarModel/index.tsx
  （1568 行：模具主表 + 记录/子件/借用子件多页签 + 图片上传）
- /home/dwp/workspace/job/sverpweb/apps/api/src/modules/prod/router.ts
  （acrylicMold/record/subItem/borrowSubItem/upload 端点）
- API 客户端 /home/dwp/workspace/job/sverpweb/apps/web/src/apis/prod.ts
参考 seed key：erp/prod（父）、erp/prod/model。权限码建议
erp:prod:model:read/write。数据层 SQLite+演示数据；文件上传参照 stable
quality 模块的文件存储实现（本地目录 + SQLite 元数据）。
```

### G6 — srm/sharing（父路由 + 1 叶）
```
G6: srm 组 — 父路由 /srm + srm/sharing（/srm/pchs/process-sharing，采购工序
共享与排期）。参考业务规格：
- /home/dwp/workspace/job/sverpweb/apps/web/src/pages/SRM/PCHS/（index.tsx +
  ProcessModal.tsx + data.ts）
- /home/dwp/workspace/job/sverpweb/apps/api/src/modules/srm/router.ts
  （po/poDetails/getPoProdPhaseData/addPoProdPhaseData/
  delPoProdPhaseData/arrangedSharedPo/crudArrangedSharedPo 端点）
- API 客户端 /home/dwp/workspace/job/sverpweb/apps/web/src/apis/srm/
参考 seed key：srm（父，顶级）、srm/sharing。权限码建议
srm:sharing:read/write。数据层 SQLite+演示数据。
```

### G7 — /m/mold/:itemCode（公开移动端）
```
G7: /m/mold/:itemCode — 公开移动端模具详情页（无需登录，走公开 API）。
参考业务规格：
- /home/dwp/workspace/job/sverpweb/apps/web/src/pages/Mobile/MoldDetail.tsx
  （978 行：模具资料/记录/子件/借用展示 + 图片）
- 参考公开端点：apps/api/src/modules/prod/router.ts 中无鉴权的 mold 查询
  （m/... 公开路径，参考 sverpweb 的独立公开路由实现）
- 参考 sverpweb apps/web/src/router 中 /m/mold/:itemCode 的公开路由配置
参考 seed key：无（公开路由不进权限菜单；在 built-in-definitions 外处理，
参照 stable 现有公开路由模式如登录页）。页面适配移动端布局，用 Iris UI
响应式组件。数据与 G5 同源（SQLite 模具台账），仅读。
```

## 每组的验收门禁（流水线内置）

- `pnpm --filter @sverp/api typecheck && test`
- `pnpm --filter @sverp/web typecheck && test && build`
- 无 dist 产物（check-no-dist-edit）
- 全部完成后更新 docs/route-migration.md 状态（[x]/[~] 带证据说明）
