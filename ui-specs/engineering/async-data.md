# Async & Data Engineering Spec — 请求限流/防抖/竞态/幂等决策表

本文件把"十年经验"写成**条件化决策表**。Agent 不得给所有请求默认加
debounce，必须按事件来源选择策略。

## 1. 请求分类

| 类型 | 含义 | 策略 |
|---|---|---|
| query | 读取数据 | 可缓存、可去重、可取消 |
| mutation | 修改数据 | pending lock，禁止自动重试 |
| command | 不可轻易撤销的业务动作（审批/支付/删除） | pending lock + 幂等键 |
| stream | 轮询/WebSocket | 生命周期管理、重连退避 |

## 2. 请求频率控制决策表

| 场景 | 策略 |
|---|---|
| 搜索输入联想 | debounce 250–400ms |
| 窗口 resize / 滚动监听 | throttle 或 requestAnimationFrame |
| 保存/提交按钮 | **禁止 debounce**，使用提交锁 |
| 删除、审批、付款 | 单次提交锁 + 幂等键 |
| 下拉加载更多 | 请求中禁止再次触发 |
| 本地输入校验 | 可 debounce |
| 用户明确点击"查询" | 直接请求 |
| 相同查询参数 | 请求去重或缓存 |
| 参数快速变化 | 取消旧请求或忽略旧响应 |

## 3. 请求竞态（强制）

筛选/搜索词/分页/路由参数触发的异步请求，**必须**防旧响应覆盖新响应：

- AbortController / CancelToken
- requestId/version token 比对
- switchMap 类操作符

禁止仅依赖 loading 布尔值解决竞态。

## 4. 重复提交

按钮 disabled 只是 UI 层。所有创建/审批/支付/删除/提交必须考虑：

1. UI pending lock
2. 服务端幂等键（`Idempotency-Key`）
3. 重试是否重复执行（非幂等写操作禁止自动重试）
4. 页面刷新后的状态恢复
5. 客户端超时但服务端已成功（uncertain 状态）

## 5. 重试矩阵

| 场景 | 自动重试 |
|---|---|
| GET 网络波动 | 可有限重试（指数退避+抖动） |
| GET 500 | 可有限重试 |
| 429 | 按 Retry-After |
| POST 创建 | 默认不自动重试 |
| 支付 | 不自动重试 |
| 文件分片上传 | 可重试当前分片 |
| 401 | 刷新令牌后最多重放一次 |
| 403 / 422 / 409 | 不重试（409 刷新数据后交给用户决策） |

## 6. 异步状态模型

读取：`idle / loading / success / empty / error / refreshing`
写入：`idle / submitting / success / error / uncertain`（uncertain = 客户端
超时但无法判断服务端是否已执行）

## 7. 禁止事项

- 单个全局 loading 控制多个无关请求
- setTimeout 模拟数据同步
- 静默吞掉 catch、仅 console.log 错误
- 旧响应覆盖新状态
- 组件卸载后继续更新本地状态
- 把服务端数据复制到多个本地状态源
- 在 UI 暴露服务端堆栈

## 8. 资源生命周期（创建必须定义释放点）

事件监听 / Timer / WebSocket / Stream / Observer / Worker /
Object URL / AbortController / FocusNode / AnimationController——
任何长期资源必须成对释放（useEffect cleanup、dispose、cancel）。
