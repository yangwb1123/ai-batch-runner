# 运营控制台（T15/T16）：任务自我修复/优化过程可视化

## 三种查看面（同一事实源：共享 run 账本 + 日志 + message 索引）

| 查看面 | 命令 | 说明 |
|---|---|---|
| TUI | `/runs` `/agents` | 最近 runs 与状态标记跨实例同源 |
| Web/手机 | `pi-batch ops serve --token X` | 默认只读监控；加 `--control` 开启受保护的会话/指令 API（stdlib，默认 127.0.0.1） |
| CLI | `ops overview / runs / log / messages / report` | 统计/明细/日志过滤/静态 HTML |

## API（ops serve）

    GET /                       自动刷新页（30s reload）
    GET /api/overview           账本统计（状态分布/按 agent/被拒计数）
    GET /api/runs?agent=&limit= 最近 runs
    GET /api/log?grep=&tail=    运营日志尾部/过滤（需 --log-file 产生）
    GET /api/messages?limit=    message 索引最近记录
    GET /api/sessions?limit=50   会话列表（历史只读；control 会话可控）
    GET /api/sessions/<id>       有界会话详情/事件
    POST /api/sessions           control 模式创建服务端会话
    POST /api/sessions/<id>/turn control 模式异步发送有界 prompt
    POST /api/sessions/<id>/cancel  control 模式请求停止本地 turn
    GET /api/jobs/<id>            查询指令状态
可选 --token：Bearer 头或 ?token=（恒定时间比较）。POST 仅在 `--control` 且有 token 时开放；
历史会话不会隐式 resume，远程监听还必须配置 TLS。

## 自我修复/优化可见性映射

- 自动修复结果：账本 status（rejected/failed/succeeded）
- 被拒诊断：data_dir/rejected/（有界摘录路径在 overview 报告）
- 运营日志：ops log / api/log
- message 记录：ops messages / memory find
- 成本/模型决策：route（难度档/降级原因，见 t14）

## 运行

    # 只读监控：
    pi-batch ops serve --port 8731 --token-file ~/.config/pbatch/ops-token &
    # 或选择 control 模式（不要与上面进程同时占用同一端口）：
    pi-batch ops serve --control --token-file ~/.config/pbatch/ops-token &
    # 浏览器打开 http://127.0.0.1:8731/?token=<token>
    # 跨局域网：再加 --host 0.0.0.0 --tls-cert server.crt --tls-key server.key
