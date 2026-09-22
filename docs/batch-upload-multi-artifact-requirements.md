# Batch Upload：同一资产多 Artifact 上传需求

日期：2026-09-22

## 1. 背景

Blender MCP 的资产制备结果不是单一文件。一个资产通常至少包含：

- 一个 `PAYLOAD`，例如 `.blend`；
- 一个 `MAIN` Preview；
- 后续还可能扩展 `FRONT`、`SIDE`、`TOP`、`BACK`、`DETAIL` 等 Preview。

AssetPlatform 会为这些文件分别生成独立的 signed PUT URL。它们属于同一个资产、同一个 AssetPlatform Upload Session，但在对象存储中仍是彼此独立的 immutable object。

当前 Blender MCP 的 `start_upload_prepared_artifacts` 要求一次 upload job 中 `itemKey` 唯一。结果是同一个资产如果同时上传 Payload 和 Preview：

```text
itemKey=boy, artifactId=<payload>
itemKey=boy, artifactId=<main-preview>
```

会被拒绝为 `UPLOAD_DUPLICATE_ITEM_KEY`。调用方只能人为拆成“Payload 一批、Preview 一批”。这与 batch upload 的语义不符，也会在一个资产拥有多张 Preview 时造成更多不必要的 upload job。

## 2. 设计原则

### 2.1 itemKey 是资产分组标识，不是上传项主键

`itemKey` 用来表达“这个 artifact 属于哪个源资产/批量制备项”。同一个资产的多个 artifact 必须允许共享同一个 `itemKey`。

### 2.2 artifactId 是 upload job 内的唯一 artifact 身份

同一个 `artifactId` 在一个 upload job 中只能出现一次。

因此：

- 允许：相同 `itemKey` + 不同 `artifactId`；
- 拒绝：相同 `artifactId` 被重复提交；
- 拒绝：重试时用已有 `artifactId` 搭配不同的 `itemKey`；
- 保持现有 batch prepare manifest 校验：`artifactId` 必须确实属于对应 `itemKey`。

这比把 `itemKey` 当唯一键更符合当前内部模型：Upload Job 的顺序、成员集合、状态表本来都以 `artifactId` 为键。

## 3. 功能需求

### FR-1 同一资产多 Artifact

一次 `start_upload_prepared_artifacts` 调用必须能够同时包含同一 `itemKey` 的多个 artifact，例如：

```text
boy / PAYLOAD
boy / MAIN_PREVIEW
chair / PAYLOAD
chair / MAIN_PREVIEW
```

四个 artifact 应进入同一个 upload job，并受同一个并发度控制。

### FR-2 多 Preview 可扩展

同一个 `itemKey` 可以继续包含多个 Preview artifact，不因 Preview 数量增加而要求拆分 upload job。

### FR-3 Artifact 唯一

一次 upload job 中 `artifactId` 必须唯一。重复 `artifactId` 继续返回稳定错误：

```text
UPLOAD_DUPLICATE_ARTIFACT
```

### FR-4 Batch Prepare 归属校验不放宽

若某个 `artifactId` 不属于其声明的 `itemKey`，仍应返回：

```text
UPLOAD_ARTIFACT_NOT_IN_BATCH_PREPARE
```

允许重复 `itemKey` 不得削弱来源归属校验。

### FR-5 Retry / Idempotency 语义保持

- 原有 `idempotency_key` 指纹行为保持；
- 重试只能重试原 Job 的 artifact；
- 同一 `artifactId` 若在重试中被改成另一个 `itemKey`，仍拒绝为 `UPLOAD_ARTIFACT_NOT_IN_JOB`；
- 已成功 artifact 不重复 PUT。

### FR-6 返回结构兼容

`get_upload_prepared_artifacts` 返回的每个 item 继续同时包含：

- `itemKey`
- `artifactId`
- `status`
- `attempts`
- 结果或错误信息

不改变现有 MCP Tool 的参数结构，不引入嵌套 `artifacts[]` 的 breaking change。

## 4. 非功能需求

- 不降低现有 signed URL / secret 脱敏能力；
- 不改变并发上限和分页机制；
- 不增加未绑定到 batch prepare manifest 的 artifact 上传能力；
- 不要求 AssetPlatform 修改 Upload Session / Finalize 契约；
- 保持现有单 artifact 上传 API 兼容。

## 5. 非目标

本次不重构 MCP 输入为：

```text
items:
  - itemKey: boy
    artifacts: [...]
```

这种结构虽然更直观，但属于接口 breaking change。当前采用最小兼容修改：保留扁平列表，只调整 `itemKey` 的唯一性语义。

## 6. 验收标准

1. RED 用例能够在旧实现上真实复现 `UPLOAD_DUPLICATE_ITEM_KEY`。
2. 修改后同一个 `itemKey` 的两个不同 artifact 可以在同一个 job 中成功 PUT。
3. Job 查询结果保留两个条目，二者 `itemKey` 相同、`artifactId` 不同。
4. 重复 `artifactId` 仍失败为 `UPLOAD_DUPLICATE_ARTIFACT`。
5. 错误的 `itemKey/artifactId` batch membership 仍失败。
6. Retry、idempotency、分页、取消、并发和 secret-redaction 现有回归全部通过。
7. Blender MCP 全量测试通过。
