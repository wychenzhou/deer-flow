# LangGraph Checkpoint 表结构详解

## 📊 表关系总览

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           checkpoints (主表)                                │
├─────────────────────────────────────────────────────────────────────────────┤
│ PK  thread_id          ──────── 会话唯一标识 (如 "user_01#session_1")       │
│ PK  checkpoint_ns      ──────── 命名空间 (通常为空字符串 "")                │
│ PK  checkpoint_id      ──────── 检查点 UUID                                │
│ FK  parent_checkpoint_id ────── 指向上一个检查点 (形成链表)                  │
│     type               ──────── 类型 (通常为 null)                          │
│     checkpoint         ──────── JSON 数据 (元数据 + 版本号)                 │
│     metadata           ──────── JSON 元数据 (step, source, model 等)        │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    │ 通过 channel_versions 中的版本号关联
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                        checkpoint_blobs (数据表)                            │
├─────────────────────────────────────────────────────────────────────────────┤
│ PK  thread_id          ──────── 会话唯一标识 (同 checkpoints)               │
│ PK  checkpoint_ns      ──────── 命名空间 (同 checkpoints)                   │
│ PK  channel            ──────── 通道名称 (如 "messages", "thread_data")     │
│ PK  version            ──────── 版本号 (来自 checkpoint.channel_versions)   │
│     type               ──────── 序列化类型 ("msgpack" 或 "json")            │
│     blob               ──────── 二进制数据 (序列化后的状态)                  │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    │ 通过 checkpoint_id + task_id 关联
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                      checkpoint_writes (写入记录表)                          │
├─────────────────────────────────────────────────────────────────────────────┤
│ PK  thread_id          ──────── 会话唯一标识 (同 checkpoints)               │
│ PK  checkpoint_ns      ──────── 命名空间 (同 checkpoints)                   │
│ PK  checkpoint_id      ──────── 检查点 UUID (同 checkpoints)                │
│ PK  task_id            ──────── 任务 UUID                                   │
│ PK  idx                ──────── 写入顺序索引 (0, 1, 2...)                   │
│     channel            ──────── 目标通道名称                                │
│     type               ──────── 序列化类型                                  │
│     blob               ──────── 二进制数据 (写入的内容)                      │
│     task_path          ──────── 任务路径 (如 "~__pregel_pull, model")        │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 🔗 字段关联详解

### 1. checkpoints → checkpoint_blobs (一对多)

**关联方式：** 通过 `channel_versions` 中的版本号

```json
// checkpoints.checkpoint 字段内容
{
  "channel_versions": {
    "messages": "00000...147.0.013378...",     // ← 这个版本号
    "thread_data": "00000...116.0.401220...",  // ← 这个版本号
    "__start__": "00000...115.0.363803..."     // ← 这个版本号
  }
}
```

```sql
-- 查询某个检查点的 messages 数据
SELECT cb.blob, cb.type
FROM checkpoints c
JOIN checkpoint_blobs cb ON 
  cb.thread_id = c.thread_id 
  AND cb.channel = 'messages'
  AND cb.version = c.checkpoint->'channel_versions'->>'messages'
WHERE c.checkpoint_id = 'xxx';
```

### 2. checkpoints → checkpoint_writes (一对多)

**关联方式：** 通过 `checkpoint_id`

```sql
-- 查询某个检查点的所有写入记录
SELECT cw.*
FROM checkpoint_writes cw
WHERE cw.checkpoint_id = 'xxx'
ORDER BY cw.task_id, cw.idx;
```

### 3. checkpoint_blobs 版本复用

**关键点：** 多个检查点可以共享同一个 blob 版本！

```
检查点 A (Step=100): channel_versions.messages = "v1"
检查点 B (Step=101): channel_versions.messages = "v1"  ← 复用！
检查点 C (Step=102): channel_versions.messages = "v2"  ← 新版本
```

---

## 📝 完整数据示例

### 示例 1: 一个会话的完整数据流

```sql
-- 假设 thread_id = 'user_01#session_1'

-- checkpoints 表 (3条记录)
┌─────────────────────┬─────────────────────┬──────────┬──────────────────────┐
│ checkpoint_id       │ parent_checkpoint_id │ step     │ channel_versions     │
├─────────────────────┼─────────────────────┼──────────┼──────────────────────┤
│ cp-001 (最早的)     │ NULL                │ 0        │ {messages: "v1"}     │
│ cp-002              │ cp-001              │ 1        │ {messages: "v1"}     │ ← 复用 v1
│ cp-003 (最新的)     │ cp-002              │ 2        │ {messages: "v2"}     │ ← 新版本
└─────────────────────┴─────────────────────┴──────────┴──────────────────────┘

-- checkpoint_blobs 表 (2条记录，不是3条！)
┌─────────────┬─────────────────────┬──────┬───────────────────┐
│ channel     │ version             │ type │ blob_size         │
├─────────────┼─────────────────────┼──────┼───────────────────┤
│ messages    │ v1                  │ msgpack │ 1024 bytes     │
│ messages    │ v2                  │ msgpack │ 2048 bytes     │
└─────────────┴─────────────────────┴──────┴───────────────────┘

-- checkpoint_writes 表 (5条记录)
┌─────────────────────┬─────────────────────┬─────┬──────────┬─────────────────┐
│ checkpoint_id       │ task_id             │ idx │ channel  │ task_path       │
├─────────────────────┼─────────────────────┼─────┼──────────┼─────────────────┤
│ cp-001              │ task-001            │ 0   │ messages │ ~__pregel_pull  │
│ cp-001              │ task-001            │ 1   │ __start__│ ~__pregel_pull  │
│ cp-002              │ task-002            │ 0   │ messages │ ~__pregel_pull  │
│ cp-003              │ task-003            │ 0   │ messages │ ~__pregel_pull  │
│ cp-003              │ task-003            │ 1   │ thread_data│ ~__pregel_pull│
└─────────────────────┴─────────────────────┴─────┴──────────┴─────────────────┘
```

### 示例 2: checkpoint JSON 结构

```json
{
  // === 基本信息 ===
  "v": 4,                           // 格式版本号
  "id": "cp-003",                   // 检查点 ID
  "ts": "2026-06-09T06:49:34",      // 时间戳

  // === 通道值 (只存储原始值，复杂对象在 blobs 表) ===
  "channel_values": {
    "title": "会话标题",             // 字符串 → 存在这里
    "count": 42,                     // 数字 → 存在这里
    "flag": true                     // 布尔 → 存在这里
    // "messages": [...]             // 列表 → 不在这里，在 blobs 表
  },

  // === 通道版本 (指向 blobs 表的版本号) ===
  "channel_versions": {
    "messages": "00000...147.0.013378...",     // → checkpoint_blobs.version
    "thread_data": "00000...116.0.401220...",  // → checkpoint_blobs.version
    "__start__": "00000...115.0.363803..."     // → checkpoint_blobs.version
  },

  // === 版本跟踪 (每个节点看到的最新版本) ===
  "versions_seen": {
    "model": {
      "branch:to:model": "00000...142.0.94452..."
    },
    "tools": {},
    "__start__": {
      "__start__": "00000...114.0.01670..."
    }
  },

  // === 更新的通道 (本次检查点更新了哪些通道) ===
  "updated_channels": ["messages", "thread_data"]
}
```

---

## 🎯 查询场景

### 场景 1: 获取某个会话的最新聊天历史

```sql
-- 步骤 1: 找最新检查点
WITH latest_checkpoint AS (
  SELECT checkpoint_id, checkpoint->'channel_versions'->>'messages' as msg_version
  FROM checkpoints
  WHERE thread_id = 'user_01#session_1'
  ORDER BY checkpoint->>'ts' DESC
  LIMIT 1
)
-- 步骤 2: 获取消息 blob
SELECT cb.blob, cb.type
FROM checkpoint_blobs cb, latest_checkpoint lc
WHERE cb.thread_id = 'user_01#session_1'
  AND cb.channel = 'messages'
  AND cb.version = lc.msg_version;
```

### 场景 2: 获取某个会话的所有检查点

```sql
SELECT 
  checkpoint_id,
  parent_checkpoint_id,
  checkpoint->>'ts' as timestamp,
  metadata->>'step' as step,
  metadata->>'source' as source,
  metadata->>'lc_agent_name' as agent_name
FROM checkpoints
WHERE thread_id = 'user_01#session_1'
ORDER BY checkpoint->>'ts' DESC;
```

### 场景 3: 查看某个检查点的所有通道数据

```sql
SELECT 
  cb.channel,
  cb.version,
  cb.type,
  LENGTH(cb.blob) as blob_size
FROM checkpoint_blobs cb
WHERE cb.thread_id = 'user_01#session_1'
  AND cb.version IN (
    SELECT jsonb_object_keys(checkpoint->'channel_versions')
    FROM checkpoints
    WHERE checkpoint_id = 'cp-003'
  )
ORDER BY cb.channel;
```

### 场景 4: 查看某个检查点的写入记录

```sql
SELECT 
  cw.task_id,
  cw.idx,
  cw.channel,
  cw.task_path,
  LENGTH(cw.blob) as blob_size
FROM checkpoint_writes cw
WHERE cw.checkpoint_id = 'cp-003'
ORDER BY cw.task_id, cw.idx;
```

---

## 💡 核心逻辑总结

### 1. **检查点链表**
```
cp-001 ← cp-002 ← cp-003
  ↑         ↑         ↑
parent   parent    parent
(NULL)   (cp-001)  (cp-002)
```

### 2. **版本号机制**
- 每个通道有独立的版本号
- 版本号 = 序列号 + 随机数
- 如果通道没有变化，版本号保持不变（复用 blob）

### 3. **数据分离存储**
- **原始值** (string, int, bool) → 存在 `checkpoints.checkpoint.channel_values`
- **复杂对象** (list, dict) → 存在 `checkpoint_blobs.blob`

### 4. **写入记录**
- 记录每次状态变更的详细信息
- 包括：哪个任务、写入了哪个通道、写入顺序

---

## 🔍 数据恢复流程

```python
def restore_state(thread_id, checkpoint_id=None):
    # 1. 获取检查点
    if checkpoint_id:
        checkpoint = get_checkpoint(thread_id, checkpoint_id)
    else:
        checkpoint = get_latest_checkpoint(thread_id)
    
    # 2. 获取版本号
    versions = checkpoint['channel_versions']
    
    # 3. 加载所有通道的 blob
    channel_values = {}
    for channel, version in versions.items():
        blob = get_blob(thread_id, channel, version)
        if blob:
            channel_values[channel] = deserialize(blob)
    
    # 4. 合并原始值
    channel_values.update(checkpoint['channel_values'])
    
    return channel_values
```

---

## 📊 数据量统计 (你的数据库)

| 表名 | 记录数 | 说明 |
|------|--------|------|
| checkpoints | 2,121 | 检查点元数据 |
| checkpoint_blobs | 1,215 | 通道数据 (比检查点少，因为版本复用) |
| checkpoint_writes | 2,880 | 写入记录 (比检查点多，因为一个检查点可能多次写入) |

---

## 🎨 可视化关系图

```
                    ┌─────────────────────┐
                    │    checkpoints      │
                    │  ┌───────────────┐  │
                    │  │ thread_id     │  │
                    │  │ checkpoint_id │───────┐
                    │  │ parent_id     │  │    │
                    │  │ checkpoint    │  │    │
                    │  │   └─channel_  │  │    │
                    │  │     versions  │  │    │
                    │  └───────────────┘  │    │
                    └─────────────────────┘    │
                            │                  │
                            │ version          │ checkpoint_id
                            ▼                  ▼
                    ┌─────────────────────┐  ┌─────────────────────┐
                    │ checkpoint_blobs    │  │ checkpoint_writes   │
                    │  ┌───────────────┐  │  │  ┌───────────────┐  │
                    │  │ channel       │  │  │  │ task_id       │  │
                    │  │ version       │  │  │  │ idx           │  │
                    │  │ blob          │  │  │  │ channel       │  │
                    │  └───────────────┘  │  │  │ blob          │  │
                    └─────────────────────┘  │  │ task_path     │  │
                                             │  └───────────────┘  │
                                             └─────────────────────┘
```
