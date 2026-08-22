# TUI + codex 核心闭环 · 使用指南（5 分钟上手）

> 现状（2026-08-22）：`pi-batch` **现在就能在真实终端使用**。agent=codex 时，
> 对话、流式正文、结构化时间线、取消、会话延续已构成完整核心闭环。

## 启动

```bash
# 在任意真实终端（SSH 亦可，宽度 ≥72 最佳）
cd /home/u1/ai-batch-runner
pi-batch                    # 或 python pi-batch.py（源码目录）
```

- 首屏下方 `Ask pi-batch…` 输入框直接输入任务（默认 agent 由 pi-batch.yaml 决定；
  先 `/agent codex` 或 `/agent pi` 切换执行器）。
- 非 TTY / 管道环境不会进入 TUI（保持无头契约）。

## 核心闭环演示（agent = codex）

```text
You:  /agent codex                      ← 切到结构化执行器（命令面板 Ctrl+P 也行）
You:  运行 pytest tests/test_tui_timeline.py 并总结结果
      [codex 执行：正文流式出现；每次工具/命令/文件变化进入时间线]
      → 按 Ctrl+T 展开时间线卡片：command/file/usage 逐项可见，成功命令折叠
You:  刚才那次运行里 FileChange 涉及哪些文件？       ← 第二轮
      [第二轮自动 resume 同一 codex 线程（thread id 已捕获），模型记得上一轮]
      → Ctrl+C 可随时中断；/status 看会话/工作区/状态；/diff 看本轮变更
```

## 已可用能力（agent=codex）

| 能力 | 入口 | 状态 |
|---|---|---|
| 对话 turn（含流式正文） | 输入框 | ✅ |
| 结构化时间线（command/file/usage 卡片） | `Ctrl+T` / `/timeline` | ✅ |
| 跨轮会话延续（上下文记忆） | 自动（provider thread resume） | ✅ 本轮接通 |
| 中断/取消（handle-scoped） | `Ctrl+C` / `/quit` | ✅ |
| 工作区变化与 diff | `Ctrl+G` / `/files` / `/diff [path]` | ✅ |
| 状态/诊断 | `/status` `/doctor` `/refresh` | ✅ |
| 记忆/会话控制 | `/memory` `/new` `/clear` | ✅ |
| 窄屏降级 | 宽 <72 隐藏侧栏/时间线 | ✅ |

## 尚未接线（诚实清单）

- **typed 审批弹窗与 sandbox 常驻显示**：App Server 协议层已就绪（T8），但本机
  codex daemon 组件缺失（`codex app-server daemon` 需额外安装）；安装后 TUI 集成
  是下一步（第二阶段批次）。
- Plan/Agents Inspector、session picker UI：Phase 1 余项，headless 路径已可用
  （`--session-mode shared` / session 映射）。
- review 入口在 headless（`pi-batch review` / `pi-batch review-fix`）已可用；
  TUI `/review` 挂接未做。
- headless 侧全部能力不受 TUI 影响：`-p`、pipeline、campaign、isolate 等照常。

## 常见问题

- **第二轮不记得上下文** → 检查 `/status` 里 agent 是否为 codex 且上一轮成功；
  线程失效时工具自动降级为新会话并在 stderr 提示 "resuming provider thread… /
  resume failed… starting fresh"。
- **时间线看不到** → 宽 <72 自动隐藏；`/timeline` 或 Ctrl+T 切换。
- **不需要 TUI** → 一切子命令照常无头运行（`pi-batch -p "..." --agent-bin codex`）。