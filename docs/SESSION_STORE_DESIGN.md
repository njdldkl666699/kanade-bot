# 会话数据库设计（Session Store v2）

> 实现位置：`kanade_bot/plugins/chat/agent/history/store.py`、`history/blobs.py`
> 版本：v6.0.3（2026-10-13 重构落地）

## 1. 概述

会话历史存储负责聊天消息的全量持久化、压缩事件标记与恢复重建。v2 重构的动机来自两个生产问题：

1. **400 → OOM 事故**（2026-10-11）：单会话 902 行 / 72.8MB 历史中 47.8MB 是 base64 图片。图片以内嵌 base64 形式参与每轮 restore 反序列化、压缩指纹计算与请求序列化，是内存锯齿与 CPU 开销的主要燃料
2. **单库共享**：所有群共用一个 SQLite 库，跨会话写竞争同一 WAL 写锁；删除数据后文件不回收空间；无法按会话独立备份/清理

v2 的三个核心变化：**每会话一个库**、**图片外置**、**restore 内存缓存**。

## 2. 布局与 Schema

```
data/chat/sessions/
├── qq-group-875082051.sqlite3
├── qq-private-123456.sqlite3
└── ...
```

- 会话 ID 满足 `^[A-Za-z0-9_-]{1,128}$` 时直接作文件名，否则取 sha256 十六进制
- 每库 WAL 模式；连接按操作建立、用完即关（无连接池、无文件句柄常驻）

```sql
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seq INTEGER NOT NULL,          -- 会话内单调递增，rollback 的回退锚点
    payload BLOB NOT NULL,         -- 单条消息的 ModelMessagesTypeAdapter JSON
    created_at REAL NOT NULL
);

CREATE TABLE compaction_marks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT NOT NULL,        -- clear_tool_results | summarizing
    params TEXT NOT NULL,          -- 压缩参数 JSON（重放依据）
    result BLOB,                   -- 摘要快照（可重放策略为 NULL）
    fingerprint TEXT NOT NULL,     -- 参数指纹，检测配置漂移
    applied_at REAL NOT NULL,
    up_to_seq INTEGER NOT NULL     -- 覆盖到该 seq（含）；-1 = 覆盖全部（旧数据兼容）
);

CREATE TABLE blobs (
    hash TEXT PRIMARY KEY,         -- sha256(mime + \0 + bytes)
    mime TEXT NOT NULL,
    data BLOB NOT NULL,            -- 图片等二进制原始 bytes
    created_at REAL NOT NULL
);
```

消息只追加（`seq = MAX(seq)+1`）；`rollback` 是唯一删除路径，只删本轮尾部行与 `up_to_seq` 越过回退点的 marks。

## 3. 图片外置

### 占位符

持久化时，消息中的 `BinaryContent`（用户图片、工具返回的图片）替换为普通字符串占位：

```
$kanade-blob:{sha256(media_type + \0 + data)}:{media_type}
```

占位是 pydantic-ai 官方类型系统内的 `str`，因此标准 `dump_json` / `validate_json` 无需任何定制；还原不依赖反序列化钩子。

### 读写边界

外置与还原只发生在 `SessionStore` 内部，上层（压缩、请求、指纹）永远看到真实 `BinaryContent`：

| 路径 | 行为 |
|---|---|
| `append` | 外置化消息 → payload 行 + blobs 表 `INSERT OR IGNORE`（内容 hash 去重，同图重发零冗余） |
| `add_compaction_mark` | result 快照同样外置化后入库 |
| `load` | validate 后还原（占位 → 查 blobs → 构造 `BinaryContent`，经 `narrow_type` 收窄为 `BinaryImage` 等子类，与官方反序列化路径一致） |
| `load_compaction_marks` | result 还原为真实 bytes，`apply_strategy` 无感知 |
| blob 缺失 | 占位原样保留（不崩溃），降级为文本 |

覆盖位置：`UserPromptPart.content` 多模态序列、`ToolReturnPart.content`（含序列内元素）。

### 孤儿 blobs

`rollback` 删除消息行后其引用的 blobs 成为孤儿，不追踪（来源少、量小）；`clear`（会话 reset）全量清空 blobs 表。

## 4. restore 缓存与快路径

`restore` 的结果缓存在会话库实例（`_ConvDB`）上：

- **缓存形态是占位形态**（不含图片 bytes），内存占用约为真实形态的 1/3；命中后经 blob LRU 还原
- **键为压缩参数指纹**：`params.fingerprint()` 不匹配不命中，防止同实例异参数读到过期结果
- **写路径失效**：`append` / `rollback` / `add_compaction_mark` / `clear_marks` / `clear` 后置空。所有写路径都在本类内，进程内强一致（bot 单进程运行，无跨进程写）
- **blob LRU**：每会话 128MB 预算，热点图片常驻内存、冷图淘汰；重启自然冷缓存

### 快路径：被支配 mark 剪枝 + 前缀跳读（v2.1）

缓存未命中时不再全量加载，而是（`store.py::prune_dominated_marks` / `_replay_pruned`）：

1. **先剪枝再重放**：边界为 B 的摘要档 mark 支配它之前边界 ≤ B 的全部 marks
   （摘要重放直接采用存储快照、完全忽略输入，被支配 marks 的产物必然被整体替换）。
   典型链 `[clear@b, sum@b, sum@b2, …]` 全部坍缩为最后一条摘要。
   剪枝是纯读路径优化，不改库（rollback 语义不变）；clear 档输出参与后续决策，不支配任何 mark
2. **漂移检测只覆盖幸存 marks**：被支配 mark 的过期指纹不参与漂移判定，
   也不得触发丢弃健康摘要的重建（413 事故教训：死 mark 指纹漂移 → `clear_marks`
   连健康摘要一起删 → 全量复活）
3. **前缀跳读**：首条幸存 mark 为摘要档时，其覆盖前缀的消息行完全不读取
   （`WHERE seq > cut`）；marks 的 `result` 懒还原（仅幸存者需要）
4. **行↔消息 1:1 假设**：快路径切片要求每行 payload 恰好一条消息、mark 边界单调
   （写路径必然满足）；旧数据破坏假设时抛 `_LegacyLayoutError` 自动回退 v2 保守全量重放

### 关键决策：marks 重放始终在真实形态上执行

缓存与快路径加载可以存占位形态，但**参与策略决策的消息必须是真实形态**：
clear 段在进入 `apply_strategy` 前经 blob LRU 还原，摘要快照在采用前还原。
原因：`ClearToolResults` 等策略内部依赖 token 估算做清理决策，占位形态会使估算
严重失真（base64 巨串 → 几十字节），重放结果与在线压缩时的决策分歧，破坏
「恢复历史 == 关闭前发送内容」这一核心不变量。被支配前缀与被摘要档丢弃的
segment 永不参与决策，保持占位形态（甚至不加载）是安全的。

## 5. 消息指纹

`_messages_fingerprint` 在 **blob 标记形态**上序列化：`BinaryContent` 替换为 `$kanade-blob:{hash}`（无需 DB）。标记由内容 hash 决定，标记形态相等 ⟺ 还原形态相等，指纹语义与全量序列化一致，但不再搬运 base64 巨串。指纹从不持久化（marks 存的是参数指纹），仅用于进程内相等性判定，算法可安全演进。

## 6. 与压缩子系统的协作

- `persist_run`：run 结束后补写压缩前消息 → 记 mark（`up_to_seq` = 补写后行数-1）→ 追加压缩后尾部消息
- `restore`：按 `up_to_seq` 分段重放/替换并拼回尾部，重建结果与最后一次请求发送的内容一致
- 对齐使用内容级公共前缀（`repair_messages` 可能改写历史，位置切片会错位）
- 参数漂移：**幸存**（剪枝后参与重放的）marks 指纹与当前参数不一致时，丢弃旧 marks
  并按新参数从全量重放。指纹仅覆盖重放相关字段（`trigger_fraction`/`keep_pairs`/
  `context_window`，见 `REPLAY_RELEVANT_FIELDS`），生成期字段变更或 schema 演进不触发漂移；
  被支配 marks 的指纹不参与判定（v2.1，见 §4）

## 7. 实测数据

v6.0.3（本地 81MB 生产副本）：

| 指标 | v1（单库内嵌 base64） | v2 |
|---|---|---|
| 事故会话 payload | 72.8MB | 24.7MB（66 图 33.9MB 进 blobs 表） |
| restore 902 条消息 | 每轮全量反序列化 | 首轮 0.37s → 缓存轮 **0.012s** |
| 压缩指纹 | 全量 JSON dump（含 base64） | blob 标记形态（文本量级） |
| 跨会话写 | 共享单库 WAL 写锁 | 各自独立库 |

v2.1（175.8MB 生产副本，2032 行 / 133 blobs 122.9MB / 4 marks，冷 restore）：

| 指标 | v2 | v2.1（剪枝+前缀跳读） |
|---|---|---|
| restore 耗时 | 14.4s | **0.3s** |
| Python 稳态增量 | +128.9MB（含 LRU 117MB 全量图） | **+31.6MB**（LRU 仅尾部 15 图） |
| Python 阶段峰值 | 357.6MB | **167.2MB** |
| RSS 峰值增量 | +371.5MB | **+159.9MB** |
| 恢复结果指纹 | 基准 | **逐字节一致**（真库回归验证） |

## 8. 历史

- v6.0.3 之前：所有会话共用 `data/chat/agent_sessions.sqlite3`（含 `conv_id` 列），图片以 base64 内嵌消息 JSON
- v6.0.3：切换为分库 + 图片外置；旧库在启动时自动迁移后归档为 `.migrated.bak`。生产迁移已完成，迁移代码已移除
- v2.1（2026-11-01）：restore 读路径优化——被支配 mark 剪枝（先于漂移检测）、
  被支配前缀跳读、marks result 懒还原、游标迭代；等价性由
  `tests/chat/test_pai_agent.py::PrunedReplayTest`（v2 全量重放基准）守卫
