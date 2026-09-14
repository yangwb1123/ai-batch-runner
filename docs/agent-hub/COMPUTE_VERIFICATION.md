# P2 计算路径验证记录

验证范围：Hub、Fabric、Gateway 调度工具、CLI/TUI、Flutter Console 与可选 Vault
证据归档。工作区代码已整合；运行场景和配置见 [COMPUTE.md](COMPUTE.md)。
最后整体验证日期：2026-09-12。
后续 P3 生态集成结果见 [ECOSYSTEM_VERIFICATION.md](ECOSYSTEM_VERIFICATION.md)。
后续 GPU 增量结果见 [GPU_VERIFICATION.md](GPU_VERIFICATION.md)。

| 检查 | 结果 |
|---|---|
| Runner `make ci` | 通过：2,850 项回归测试、56 项门禁自测、53 项规则评估；发布检查、组织规范、能力注册表和无环依赖图检查通过 |
| Runner `python quality.py --strict .` | 通过 |
| 工程门禁 `make check` | 8 个门禁通过 |
| Hub/Fabric 最终专项 | 59 项通过，含真实双 HTTP 服务与 Runner 子进程执行、持续心跳、运行中取消 |
| Vault REST 契约 | 10 项通过，使用本地 HTTP 替身，未连接生产 Vault |
| Compute TUI | 7 项交互测试通过 |
| OpenAPI | YAML 和新增 schema 合法；4 个实际 Store/资源 DTO 样本通过 Draft7Validator 校验 |
| Console Flutter 全量测试 | 1,268 项通过；随后补充的边界与 UTF-8 用例所在定向组 22 项通过 |
| Console `flutter analyze --no-pub` | 通过，无问题 |
| Console `flutter build web --no-pub` | 通过；已将相同源码构建的产物同步到主 Console 工作区 `build/web/` |
| Console 改动页面 UI spec/质量 | 通过 |
| Console 全仓 `make ui-check` | 仍有 8 个既有间距问题，文件与 P0/P1 记录一致 |

关键验证包括：按目标实例授权过滤、CPU/内存准入、默认零内存预算、运行时
探测、并发幂等、丢失 ACK 后重启恢复、原服务身份绑定、退出后撤销的本地工具
能力、发起用户与 Gateway 权限交集、turn actor 归属、写/取消权限不泄露结果、
用户/租户入队配额、单 Runner 执行槽、慢归档不阻塞计算和取消、mTLS 与禁止重定向。

两项真实计算集成运行在临时目录和 loopback 端口：一项确认命令只执行一次并
返回 accepted evidence digest；另一项确认长任务仍持续心跳，取消后停止进程，
释放预留 CPU，且不产生取消后的文件。没有调用付费模型或修改真实外部账号。

本轮审查修复了 turn 关联归属、mutation 回执输出泄露和共享队列隔离三个问题，
均有回归用例。Fabric 回归同时修复了禁止重定向后 HTTPS context 的传递方式。
Campaign 的 4 个旧测试改为显式注入假 Runner，且补充无项目本地入口时的
工具入口回退测试；46 项通过。主工作区已有的 Campaign 入口实现修改保持原样。
旧 SSH 取消测试补上 finally 清理，防止模拟进程残留使后续 Runner 被误判为忙碌；
该测试与两项真实计算集成按原顺序复测，3 项全部通过。

## 尚未执行或实现

- 未部署实际 Snaplink OAuth client/resource/grants、Hub 或远端 Runner，也未进行跨公网实机验收。
- 未在真实 Aero Vault 服务上传文件；固定版本 REST 行为依据仓库契约以 HTTP 替身验证。
- 工作区需预先配置；本次 P2 验证时未实现 Vault 不可变快照传输、GPU 调度或
  OS 资源强制限额。GPU 的后续实现与验证见本页顶部链接。
- 此记录对应 P2；Aero ID、IM、Audit 的后续接入见 P3 验证记录。多控制面高可用仍未实现。
- 未构建 Android/iOS/桌面安装包；已验证共享 Flutter 代码和 Web 构建。

历史验证及既有 Console 问题明细见 [VERIFICATION.md](VERIFICATION.md)。
