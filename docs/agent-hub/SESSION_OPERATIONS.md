# 跨端查看会话请求记录

创建和关闭会话的请求保存在 Hub。CLI、TUI 和 Console 可按实例找回已受理的
请求，查看当前结果，再显式打开对应会话。更换客户端、重新登录或重启 Hub 后，
查询仍从持久账本读取，不依赖原设备保存 request ID 或幂等 key。

## 使用

```sh
pi-batch fleet session-operations --instance alpha --limit 20 --json
pi-batch fleet session-operations --instance alpha --before OPAQUE_CURSOR --json
pi-batch fleet session-request CREATION_REQUEST_ID --json
pi-batch fleet session-close-request CLOSE_REQUEST_ID --json
pi-batch fleet show SESSION_ID --json
```

第一条读取最近一页。`next_cursor` 非空时，可原样传给 `--before` 查看更早一页；
为空表示当前没有更多记录。省略 `--instance` 时，CLI 查询当前主体有权读取的
全部实例。每次调用只读取一页，默认 20 条，最多 50 条。

TUI 和 Console 的 **Session requests / 会话请求记录** 入口固定查询打开时的
实例，包括尚无会话的实例和离线实例。可刷新最近记录、查看更早一页、选择请求
刷新状态，以及在存在 session ID 时点击 **Open session / 打开会话**。
刷新记录不会自动切换会话；弹窗关闭或选择变化后到达的旧响应不会覆盖当前选择。
历史会话是否可继续发送 prompt，仍由查询得到的会话状态决定。

## 状态与恢复边界

| 操作 | 状态 | 含义 |
|---|---|---|
| create | queued | 已受理，等待 Gateway 创建 |
| create | created | 创建成功，可按 session ID 定位会话 |
| close | queued | 已受理，等待实际关闭；容量仍保留 |
| close | closed | 已确认关闭，历史只读，容量已释放 |
| 任一 | failed | 执行失败，固定错误码说明原因 |
| 任一 | lost | Gateway 已换代，旧请求未获得成功确认 |

列表和状态刷新只执行 GET，不重新提交创建、关闭或 prompt，不修改本地正在
追踪的未知请求及其 key。创建记录保存请求名称；关闭记录以目标 session ID
定位，不复制会话名称。列表不返回原幂等 key、发起者身份、机器 generation、
本地会话 ID、项目路径或凭据。

如果原提交的响应丢失，但 Hub 已受理，请求可在列表中找回。只有实际受理的
请求才有记录；没有查询到记录不能证明原请求未受理，也不能据此自动生成新 key
重发。仍有原 key 时，按[创建](SESSION_CREATION.md)或[关闭](SESSION_CLOSE.md)
说明沿用原目标和 key 恢复。原提交尚在网络中、权限变化或列表没有翻完，都可能
使当前页暂时看不到它。

关闭失败时不会回收容量，需按既有 Gateway 恢复流程处理。请求中心不提供强制
关闭、重新打开、删除历史或迁移会话。旧 Hub 缺少列表接口时，客户端显示不支持
请求记录；原有会话和请求详情功能继续使用。

## 权限与分页

查询需要 `agent.sessions:read` 及实例 grant；启用 Aero ID 成员门禁时还需通过
成员资格检查。跨端客户端仍需各自拥有合法 Hub 身份和授权。记录沿用已有请求
详情接口的实例共享读取规则，不按创建者或 OAuth client 私有分组；不将不同
客户端的 pairwise subject 自动映射为同一个人。Gateway 机器身份不能调用此入口。

服务端在分页前按 tenant 和实例 grant 过滤。离线、冻结和旧 generation 的记录
仍可读取，不需要 Gateway 在线。每页在同一 SQLite 读事务中，分别从两个既有
账本最多读取 `limit + 1` 条，再进行有界合并；新增查询索引，不增加第三套状态表、
回填任务或请求写入流程。

排序为不可变的 `(created_at, operation, request_id)` 倒序；状态更新不改变记录
位置。游标为有版本和长度限制的不透明值，客户端原样传递，不依赖浮点时间精度。
这是一份实时历史列表，跨页状态没有冻结：新受理请求通常在刷新最近页后出现；
系统时钟回退时，新记录按记下的时间排入更早区间，应重新刷新、翻页核对。

接口为 `GET /api/v1/agent/session-operations`，返回
`{data: [...], next_cursor: "..."}`，完整字段见 [openapi.yaml](openapi.yaml)。
每条记录以 `(operation, request_id)` 标识；客户端限制响应大小和单页数量，校验
操作与状态组合、目标绑定及重复项。游标只决定读取位置，不能扩大授权。

本机检查、基线差异和未执行的设备验收见
[SESSION_OPERATIONS_VERIFICATION.md](SESSION_OPERATIONS_VERIFICATION.md)。
