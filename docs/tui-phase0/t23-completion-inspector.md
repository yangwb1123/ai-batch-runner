# T23：Completion Inspector

状态：已交付 current-turn typed evidence 的独立只读完成视图。

## 正向流程

1. 用户在 turn 运行中或结束后执行 `/completion`，也可从命令面板打开。
2. Controller 从当前 immutable `TUIViewSnapshot` 读取同一 run 的 canonical events、
   Timeline、usage 与 authoritative after-turn workspace snapshot。
3. Inspector 分栏展示 Outcome、Commands、Changes、Unfinished plan、Validation、
   Gate verdicts、Tools、Usage、Artifacts、Risks and diagnostics。
4. 活动 turn 中的新 typed event 原位刷新 overlay；终态及后置权威 Git snapshot 到达后，
   状态和变化证据继续收口，不向 conversation transcript 追加检查文本。
5. journal 恢复复放相同 canonical events，因此终态 session 可重建同一 Completion。

## 证据纪律

- command 状态只来自 `command.*` 与显式 exit code；Assistant 正文不参与。
- validation/gate 只来自对应 typed event。没有事件时明确显示无证据，不声称 PASS。
- Changes 只采用 `extensions.authoritative=true` 的 after-turn workspace delta；
  provider `file.changed` 不能替代本地 Git 权威事实。
- 未执行项只表达为最新结构化 Plan 中尚未 completed 的步骤，不从自然语言猜测。
- risk 来自失败/未完成 command、tool、validation、gate、terminal reason 与
  runtime warning/error；“未见显式风险”同时注明不构成安全保证。
- artifact 当前只展示有界引用，不读取正文或触发外部动作。

## 有界合同

- command/tool/validation/gate/artifact/Plan：每类最多 32 条；
- runtime diagnostics：最多 16 条；authoritative paths：最多 64 条；
- usage：最多 32 个有限数值 key，NaN/Inf 丢弃；
- payload label/detail 分别限制为 256/1024 bytes；
- 截断的具体 section 在 `Evidence limits` 中点名；runtime recent-event 起点缺失时
  额外标记 event window，不把残缺窗口冒充完整报告。

## 验证

```bash
python -m pytest tests/test_tui_completion.py tests/test_tui_controller.py -q
python quality.py pbatch/tui_completion.py pbatch/tui_controller.py
```
