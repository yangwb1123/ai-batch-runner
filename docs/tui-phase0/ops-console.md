# 运营控制台（T15/T16）：任务自我修复/优化过程可视化

## 三种查看面（同一事实源：共享 run 账本 + 日志 + message 索引）

| 查看面 | 命令 | 说明 |
|---|---|---|
| TUI | `/runs` `/agents` | 最近 runs 与状态标记跨实例同源 |
| Web/手机 | `pi-batch ops serve --token X` | 只读 HTTP：自动刷新监控页 + JSON API（stdlib，默认 127.0.0.1） |
| CLI | `ops overview / runs / log / messages / report` | 统计/明细/日志过滤/静态 HTML |

## API（ops serve）

    GET /                       自动刷新页（30s reload）
    GET /api/overview           账本统计（状态分布/按 agent/被拒计数）
    GET /api/runs?agent=&limit= 最近 runs
    GET /api/log?grep=&tail=    运营日志尾部/过滤（需 --log-file 产生）
    GET /api/messages?limit=    message 索引最近记录
可选 --token：Bearer 头或 ?token=（恒定时间比较）；只读无写端点。

## 自我修复/优化可见性映射

- 自动修复结果：账本 status（rejected/failed/succeeded）
- 被拒诊断：data_dir/rejected/（有界摘录路径在 overview 报告）
- 运营日志：ops log / api/log
- message 记录：ops messages / memory find
- 成本/模型决策：route（难度档/降级原因，见 t14）

## 运行

    pi-batch ops serve --port 8731 --token mytoken &   # 后台监控
    # 浏览器打开 http://127.0.0.1:8731/
