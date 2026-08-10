# Structured Artifact Validation

## 1. 目的

LLM 输出 JSON 不代表结构正确。凡是下游会自动消费、排序、执行或批准的结构化
产物，应在原子落盘前通过确定性 schema 门禁，不得依赖 prompt 自觉。

## 2. 声明方式

任务或阶段使用现有 validator 字段声明 schema：

```yaml
validate: "jsonschema=schemas/direction.schema.json"
```

```yaml
validate_cmd: "jsonschema={tool_root}/examples/schemas/direction.schema.json"
```

CLI 等价形式：

```bash
pi-batch tasks.yaml --validate 'jsonschema=schemas/direction.schema.json'
python scripts/check-json-schema.py --schema schemas/direction.schema.json output.json
```

schema 相对路径按任务工作目录解析；`{tool_root}` 可引用随 wheel 分发的 schema。
该 validator 与其他 validator 保持 AND 语义，并参与 `--reuse` 复验。
声明式入口实际调用 `pbatch/json_schema_cli.py`，因此只复制入口、`pbatch/` 和
`pi-batch.yaml` 的轻量部署不依赖额外 `scripts/` 目录；standalone 脚本只是别名。

## 3. Fail-closed 约束

- 产物必须是纯 JSON；Markdown fence、前后说明、NaN 和 Infinity 均拒绝
- schema 和产物默认各不超过 2 MiB，可通过脚本参数降低或显式提高
- 仅允许本地 `$ref` JSON Pointer；外部网络引用拒绝
- 未支持的校验关键字、错误关键字类型、无效正则和无法解析的 `$ref` 均拒绝
- 最多输出有界错误列表；退出码和 `{"status":"fail"}` 同时反馈给重试

## 4. 支持范围

支持 type、required、properties、patternProperties、additionalProperties、
dependentRequired、dependentSchemas、propertyNames、items、prefixItems、contains、
数组/字符串/对象/数值边界、uniqueItems、pattern、enum、const、allOf、anyOf、
oneOf、not、if/then/else、本地 `$ref`，以及 date/time/date-time/email/hostname/
IP/URI/UUID format。

这不是对完整 JSON Schema 标准的虚假声明。需要当前子集之外的关键字时，应先
扩展 checker 和回归证据，或声明项目自己的外部 validator 命令。
