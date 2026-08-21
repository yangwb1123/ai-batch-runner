# 配置治理

配置来源按以下优先级合并：

```text
CLI/runtime override > project pi-batch.yaml > user settings.yaml > built-in defaults
```

项目配置由入口脚本目录优先定位，其次是 `pbatch/`、当前工作目录和工具资产目录；用户配置
固定为 XDG 目录下的 `settings.yaml`。项目和用户同一 section 内按键合并，项目键覆盖用户键。
`validators` 也遵循这条规则：用户可以提供跨项目门禁，项目可以用同名键覆盖它；执行时使用的
注册表与 `config explain` 展示的合并配置一致。

验证器可以声明适用条件，例如：

```yaml
validators:
  cargocheck:
    cmd: cargo check --quiet
    scope: repo
    requires_any: [Cargo.toml]
```

`requires_any` 是相对于目标仓库根目录的文件或 glob 列表；至少一个文件存在时才执行验证器。
条件不满足时不会运行无意义的命令，而会在日志、campaign 预检和 `config explain` 中记录为
`not applicable`/`SKIPPED`。一旦前置文件存在，验证器仍按普通门禁执行，失败会阻断流程。

## 检查命令

```bash
pi-batch config explain
pi-batch config explain --json
pi-batch config check --file path/to/pi-batch.yaml --json
```

`explain` 是只读报告，包含：

- 每个来源的路径、是否加载和读取错误；
- 递归 provenance，说明某个配置键来自 project、user 或 mixed；
- 脱敏后的合并文件配置；
- 当前进程解析出的运行时值，以及会覆盖文件默认值的 CLI 参数契约。

其中 `resolved_runtime.validators.specs` 会展示每个已解析验证器的 `scope`、`judge` 和
`requires_any`，便于确认某个跨语言门禁为什么执行或跳过。

运行时状态也遵循同一个 `data_dir`：记忆索引、被拒产物、真值/因果账本、设备状态、
Pinned 上下文和预算文件均写入该目录；未设置时仍默认为项目内 `.pi-batch/`。项目级证据
（例如 `docs/advance/`、决策日志和交付物）仍保留在项目目录中，不会因为迁移运行时目录而
丢失。`isolate --copy-memory` 会把父项目的运行时状态复制成 worktree 的本地快照，并让
子进程继续使用该快照，避免多个 worktree 共享同一份可变账本。

## 版本策略

当前 `config_version` 为 1。缺少字段的旧配置视为 v0 并给出 warning；v0 和 v1 均可读。
版本字段类型错误、负数和未来版本会使 `config check` 返回非零；运行时加载未来版本也会拒绝，
避免新字段被旧 runner 静默误读。

版本升级时应先增加迁移/兼容读取，再提升 `CONFIG_SCHEMA_VERSION`，并为旧版本、未来版本和
敏感字段脱敏各保留回归测试。
