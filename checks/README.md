# checks — 仓库工程门禁自测

> 147+ 个测试守护仓库门禁（`python -m pytest checks/`）。
> 每个测试对应一个门禁行为（检查器/验证器/schema 的确定性行为）。

## 内容

- 检查器行为测试（check-ui-spec / check-frontend-quality / check-backend-quality 等）
- 验证器注册与调用测试
- 门禁失败语义测试（fail-closed 路径）

## 维护

门禁行为变更（豁免规则/新检查器）必须在 checks/ 有对应测试；
新增门禁先写测试再实现（或并行），保持 100% 行为覆盖。
