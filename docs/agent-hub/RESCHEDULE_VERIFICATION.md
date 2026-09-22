# 排队任务重新调度验收

本增量把排队任务的目标设备变更收敛为一个原子、可审计的 Hub 操作。任务创建者
可在 `queued` 状态下选择原始授权集合内的设备，或用空目标恢复自动调度；已经
进入 `dispatching`、`running` 或终态的任务不会被迁移。

## 验证结果

| 检查 | 结果 |
|---|---|
| Compute 服务端、HTTP、active-turn broker、Python TUI 定向回归 | 43 passed |
| 触及文件 `quality.py --strict` | passed |
| Console Agent API、任务组件、GPU、刷新、工作区、placement 关联回归 | 153 passed |
| `flutter analyze --no-pub` | passed |
| Console `dart format` | passed |

新增回归覆盖任务创建者与非创建者、任务写 scope、原始目标集合与项目绑定、
非法 idempotency key、缺少目标字段、重复目标幂等、旧派发句柄清理、queued-only
状态转移、HTTP 202 响应、active-turn capability，以及 Flutter 任务卡片入口。

## 门禁限制

首次 `make ci` 未设置隔离 worktree 的导入路径，出现 23 个失败和 6 个错误；补齐
`PYTHONPATH`、`setuptools` 后重跑，4143 项中 4142 项通过，仅一个既有 TUI
时序用例偶发失败，单独重跑通过。本增量相关的 Compute/placement 回归均通过；
`make ci` 因这一次偶发失败退出，未把完整门禁标记为通过，也没有因此改动无关
测试或工程配置。

没有执行真实 OAuth、跨主机 Fabric、真实 CUDA、HA 或移动物理设备验收；目标
迁移、checkpoint 转移和在线运行中迁移仍属于后续能力。
