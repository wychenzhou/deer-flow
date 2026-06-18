-- ============================================================
-- LangGraph Checkpoint 查询 SQL 集合
-- ============================================================

-- 1. 列出所有 session（按最后活动时间排序）
-- ============================================================
SELECT
    thread_id,
    COUNT(*) as checkpoint_count,
    MIN(checkpoint->>'ts') as first_checkpoint,
    MAX(checkpoint->>'ts') as last_checkpoint,
    metadata->>'lc_agent_name' as agent_name
FROM checkpoints
GROUP BY thread_id, metadata->>'lc_agent_name'
ORDER BY last_checkpoint DESC
LIMIT 20;


-- 2. 查询某个 session 的所有检查点
-- ============================================================
-- 将 'YOUR_THREAD_ID' 替换为实际的 thread_id
SELECT
    checkpoint_id,
    parent_checkpoint_id,
    checkpoint->>'ts' as timestamp,
    checkpoint->>'v' as version,
    metadata->>'step' as step,
    metadata->>'lc_agent_name' as agent_name,
    metadata->>'source' as source,
    checkpoint->'updated_channels' as updated_channels
FROM checkpoints
WHERE thread_id = 'YOUR_THREAD_ID'
ORDER BY checkpoint->>'ts' DESC;


-- 3. 查询某个 session 的 blob 数据（按通道分组）
-- ============================================================
SELECT
    channel,
    COUNT(*) as version_count,
    MAX(version) as latest_version,
    MAX(LENGTH(blob)) as max_blob_size,
    type as serialization_type
FROM checkpoint_blobs
WHERE thread_id = 'YOUR_THREAD_ID'
GROUP BY channel, type
ORDER BY channel;


-- 4. 查询某个 session 的写入记录
-- ============================================================
SELECT
    checkpoint_id,
    task_id,
    idx,
    channel,
    type,
    LENGTH(blob) as blob_size,
    task_path
FROM checkpoint_writes
WHERE thread_id = 'YOUR_THREAD_ID'
ORDER BY checkpoint_id, task_id, idx
LIMIT 50;


-- 5. 查询最新的检查点详情（包含完整 JSON）
-- ============================================================
SELECT
    checkpoint_id,
    checkpoint->>'ts' as timestamp,
    metadata,
    checkpoint
FROM checkpoints
WHERE thread_id = 'YOUR_THREAD_ID'
ORDER BY checkpoint->>'ts' DESC
LIMIT 1;


-- 6. 查询某个检查点的所有 blob 数据
-- ============================================================
SELECT
    channel,
    version,
    type,
    LENGTH(blob) as blob_size,
    blob
FROM checkpoint_blobs
WHERE thread_id = 'YOUR_THREAD_ID'
  AND version LIKE 'YOUR_VERSION_PREFIX%'  -- 使用 checkpoint 中的 channel_versions
ORDER BY channel;


-- 7. 统计各表的数据量
-- ============================================================
SELECT
    'checkpoints' as table_name,
    COUNT(*) as row_count
FROM checkpoints
UNION ALL
SELECT
    'checkpoint_blobs',
    COUNT(*)
FROM checkpoint_blobs
UNION ALL
SELECT
    'checkpoint_writes',
    COUNT(*)
FROM checkpoint_writes;


-- 8. 查询某个 agent 的所有 session
-- ============================================================
SELECT
    thread_id,
    COUNT(*) as checkpoint_count,
    MAX(checkpoint->>'ts') as last_activity
FROM checkpoints
WHERE metadata->>'lc_agent_name' = 'YOUR_AGENT_NAME'
GROUP BY thread_id
ORDER BY last_activity DESC;


-- 9. 查询最近的错误或异常（通过 metadata 检查）
-- ============================================================
SELECT
    thread_id,
    checkpoint_id,
    checkpoint->>'ts' as timestamp,
    metadata
FROM checkpoints
WHERE metadata->>'source' = 'error'
   OR metadata->>'error' IS NOT NULL
ORDER BY checkpoint->>'ts' DESC
LIMIT 10;


-- 10. 查询特定通道的最新数据
-- ============================================================
-- 例如查询 messages 通道的最新内容
SELECT
    thread_id,
    channel,
    version,
    type,
    LENGTH(blob) as blob_size
FROM checkpoint_blobs
WHERE channel = 'messages'
  AND thread_id = 'YOUR_THREAD_ID'
ORDER BY version DESC
LIMIT 10;


-- 11. 清理旧数据（谨慎使用！）
-- ============================================================
-- 删除某个 session 的所有数据
-- DELETE FROM checkpoint_writes WHERE thread_id = 'YOUR_THREAD_ID';
-- DELETE FROM checkpoint_blobs WHERE thread_id = 'YOUR_THREAD_ID';
-- DELETE FROM checkpoints WHERE thread_id = 'YOUR_THREAD_ID';


-- 12. 查询数据大小统计
-- ============================================================
SELECT
    thread_id,
    pg_size_pretty(SUM(pg_column_size(checkpoint))) as checkpoint_size,
    COUNT(*) as checkpoint_count
FROM checkpoints
GROUP BY thread_id
ORDER BY SUM(pg_column_size(checkpoint)) DESC
LIMIT 10;
