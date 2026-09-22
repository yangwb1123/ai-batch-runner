# 按需新建远端会话

已登录的用户可选择一个在线实例，创建新的 Agent 会话，然后继续使用原有的
会话列表、prompt、事件和计算接口。创建请求和实际可用的会话分别呈现：Hub
返回 `queued` 表示已受理，只有 Gateway 确认 `created` 后才能发送 prompt。

## 使用

Gateway 的项目目录、Agent、provider、model 和计算工具仍由设备操作员配置。
可以启动一个空实例，等待各端按需创建：

```sh
pi-batch fleet agent --instance-id alpha --cwd /path/to/project \
  --sessions 0 --max-sessions 8

pi-batch fleet session-create alpha --name '项目分析' \
  --idempotency-key analysis-session-001 --json
pi-batch fleet session-request REQUEST_ID --json
pi-batch fleet sessions --instance-id alpha --json
pi-batch fleet send SESSION_ID '分析项目入口' --idempotency-key prompt-001
```

Gateway 沿用独立机器认证；客户端沿用用户设备登录或私有用户 token 文件。
创建需要 `agent.sessions:write` 及目标实例 grant，查询请求需要
`agent.sessions:read` 及同一实例 grant。启用 Aero ID 门禁时也要通过成员检查。
不增加 OAuth scope，也不自动授予目标设备、项目或计算权限。

CLI 先把本次 key 输出到 stderr。未收到创建回执时，用同一实例、名称和 key
重试；新请求使用新 key。`session-request` 返回当前状态，不阻塞等待 Gateway。
创建成功后的会话 ID 与其他会话相同，可直接用于原有命令。

TUI 的实例选择下方提供名称和 New session 入口，即使实例尚无会话也可使用；
选择 `All instances` 可在一个列表中查看有权限的多个实例，会话名称前显示来源
实例。该聚合视图仍按全局 session_id 发送 prompt，但创建、关闭和实例专属请求
入口会保持禁用，必须先选择具体实例。
Console 的会话目录同样提供新建入口，并显式选择目标实例与名称；Web、App 和
Mobile 共享该界面。客户端保留未确认请求的 key，已受理后只查询状态，不自动
重放创建 POST。用户期间切换了实例或会话时，迟到的成功回执不会抢占当前选择。
界面中的请求追踪保存在当前进程/页面内，关闭客户端后仍可用 CLI 按回执 ID 查询。

## 容量与恢复

- 新 Gateway 默认预建 1 个会话，总容量 8；`--sessions` 可选 0 到总容量，
  `--max-sessions` 可选 1 到 8。预建完成后才对外公布 `session_capacity`，避免
  启动期间预建与远程请求争抢名额。旧 Gateway 未声明此字段时，新建入口不可用。
- Hub 在同一 SQLite 事务内计算当前 generation 的已登记会话和待创建请求。
  容量满、离线或不支持创建的实例会明确拒绝。Gateway 同时检查本地容量。
  已创建会话占用名额，直到 Gateway 确认关闭成功；关闭流程与失败恢复见
  [SESSION_CLOSE.md](SESSION_CLOSE.md)。历史会话不会被删除。
- 创建请求按 tenant、subject、client_id 和幂等 key 唯一；名称去除首尾空白，
  拒绝控制字符，最多 256 UTF-8 字节。相同 key 改变实例或名称会冲突。
  重复请求返回原请求当前状态，不能靠重试绕过失败或实例重启。
- Hub 固定请求的 generation、local session ID、名称和 project。远程用户不能
  指定 cwd、模型、provider、已有会话 ID 或恢复参数。Gateway 的成功回执在一个
  事务内完成会话登记和请求状态更新，未确认前会话不可控。
- Gateway 在创建前写入本地收据，后台单 worker 创建会话和发送回执，使心跳、
  prompt 与取消处理可继续运行。同一进程重收请求或丢失 ACK 时复用已有对象，
  不再次调用会话工厂。失败只返回固定错误码。
- 状态为 `queued → created/failed/lost`，终态不可改写。工厂失败释放未完成的
  容量预留；需要再次尝试时创建新请求。Hub 重启可继续已有队列；Gateway 换代
  将旧 pending 请求标为 `lost`，不会隐式恢复或重新创建旧会话。
- HTTP 失败、401 或收到无效回执时，客户端不能据此断言请求从未受理。
  保留请求标识，重新登录后查询或手动沿原 key 重试。明确的准入拒绝可重新选机。
  创建请求记录与历史会话目前不自动删除，运营方仍需监控数据库容量。

## 协议与数据所有权

`POST /api/v1/agent/instances/{instance_id}/sessions` 接受 `{name}`，返回
202 和 `{data: request}`；`GET /api/v1/agent/session-requests/{request_id}`
读取状态。两者属于用户权限入口。Gateway 使用单独的
`/api/v1/agent/gateway/session-requests` GET/POST，绑定实例与 generation。
完整 DTO 见 [openapi.yaml](openapi.yaml)。

领域模型为创建请求及其单向状态转换；SQLite 新增独立请求表，保存主体/客户端、
幂等 key、固定执行身份和结果。唯一约束保护幂等性与 local session ID，索引覆盖
实例/generation 的待处理队列。旧数据库采用增量建表，不重写已有会话或 turn。
公开 DTO 仅有 request/instance/session ID、名称、状态、固定错误码和时间，
不包含认证主体、凭据或设备路径。会话工厂和模型执行仍由目标设备负责。

本增量的本机验证与未执行部署项见 [SESSION_CREATION_VERIFICATION.md](SESSION_CREATION_VERIFICATION.md)。
