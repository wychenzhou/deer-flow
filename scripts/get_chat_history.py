#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""获取会话的聊天历史记录

用法:
    # 使用 LangGraph API (推荐)
    python scripts/get_chat_history.py <thread_id>
    python scripts/get_chat_history.py <thread_id> --limit 10
    python scripts/get_chat_history.py <thread_id> --json

    # 直接查询数据库
    python scripts/get_chat_history.py <thread_id> --direct
"""

import asyncio
import json
import sys
from datetime import datetime

# Windows 终端编码修复
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

DB_URL = "postgresql://postgres:postgres@101.34.84.149:15432/deer-flow"


def format_time(ts_str):
    if not ts_str:
        return "N/A"
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except:
        return ts_str


def format_message(msg, index):
    """格式化单条消息"""
    # 获取消息类型
    msg_type = getattr(msg, 'type', 'unknown')

    # 获取内容
    content = getattr(msg, 'content', '')
    text = ''
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get('type') == 'text':
                text += part.get('text', '')
            elif isinstance(part, str):
                text += part
    elif isinstance(content, str):
        text = content

    # 获取工具调用 (注意: tool_calls 是字典列表，不是对象列表)
    tool_calls = getattr(msg, 'tool_calls', []) or []

    # 格式化输出
    lines = []
    if msg_type == 'human':
        lines.append(f"👤 用户 [{index}]:")
        lines.append(f"   {text[:200]}{'...' if len(text) > 200 else ''}")
    elif msg_type == 'ai':
        if tool_calls:
            lines.append(f"🤖 助手 [{index}]: (调用工具)")
            for tc in tool_calls:
                # tool_calls 是字典列表，使用字典访问方式
                if isinstance(tc, dict):
                    name = tc.get('name', 'unknown')
                    args = tc.get('args', {})
                else:
                    # 兼容对象访问方式
                    name = getattr(tc, 'name', 'unknown')
                    args = getattr(tc, 'args', {})
                lines.append(f"   📞 {name}({json.dumps(args, ensure_ascii=False)[:100]})")
        else:
            lines.append(f"🤖 助手 [{index}]:")
            lines.append(f"   {text[:200]}{'...' if len(text) > 200 else ''}")
    elif msg_type == 'tool':
        tool_name = getattr(msg, 'name', 'unknown') or 'unknown'
        lines.append(f"🔧 工具 [{index}]: {tool_name}")
        lines.append(f"   {text[:150]}{'...' if len(text) > 150 else ''}")
    else:
        lines.append(f"❓ {msg_type} [{index}]:")
        lines.append(f"   {text[:150]}{'...' if len(text) > 150 else ''}")

    return '\n'.join(lines)


async def get_chat_history_api(thread_id, limit=None, output_json=False):
    """使用 LangGraph API 获取聊天历史"""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.graph import StateGraph, END
    from typing import TypedDict, Annotated
    from langgraph.graph.message import add_messages

    # 定义状态 (用于创建 graph)
    class State(TypedDict):
        messages: Annotated[list, add_messages]

    # 创建一个简单的 graph (只需要 checkpointer)
    def dummy_node(state: State):
        return state

    builder = StateGraph(State)
    builder.add_node("node", dummy_node)
    builder.set_entry_point("node")
    builder.add_edge("node", END)

    async with AsyncPostgresSaver.from_conn_string(DB_URL) as checkpointer:
        graph = builder.compile(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": thread_id}}

        # 获取最新的 state (包含消息)
        state = await graph.aget_state(config)
        if not state:
            print(f"❌ 未找到 thread_id={thread_id} 的状态")
            return

        messages = state.values.get('messages', [])
        if limit:
            messages = messages[-limit:]

        if output_json:
            # 输出 JSON 格式
            result = {
                "thread_id": thread_id,
                "checkpoint_id": state.config.get("configurable", {}).get("checkpoint_id"),
                "metadata": state.metadata,
                "messages": []
            }
            for msg in messages:
                msg_data = {
                    "type": getattr(msg, 'type', 'unknown'),
                    "content": getattr(msg, 'content', ''),
                }
                tool_calls = getattr(msg, 'tool_calls', []) or []
                if tool_calls:
                    # tool_calls 是字典列表
                    msg_data["tool_calls"] = [
                        {
                            "name": tc.get('name', 'unknown') if isinstance(tc, dict) else getattr(tc, 'name', 'unknown'),
                            "args": tc.get('args', {}) if isinstance(tc, dict) else getattr(tc, 'args', {}),
                            "id": tc.get('id', None) if isinstance(tc, dict) else getattr(tc, 'id', None),
                        }
                        for tc in tool_calls
                    ]
                tool_call_id = getattr(msg, 'tool_call_id', None)
                if tool_call_id:
                    msg_data["tool_call_id"] = tool_call_id
                result["messages"].append(msg_data)

            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            # 格式化输出
            print(f"\n{'='*80}")
            print(f"📋 会话聊天历史 (LangGraph API)")
            print(f"{'='*80}")
            print(f"Thread ID: {thread_id}")
            print(f"Checkpoint ID: {state.config.get('configurable', {}).get('checkpoint_id', 'N/A')[:20]}...")
            print(f"Step: {state.metadata.get('step', 'N/A')}")
            print(f"Source: {state.metadata.get('source', 'N/A')}")
            print(f"消息数量: {len(messages)}")
            print(f"{'='*80}\n")

            for i, msg in enumerate(messages, 1):
                print(format_message(msg, i))
                print()


def get_chat_history_direct(thread_id, limit=None, output_json=False):
    """直接查询数据库获取聊天历史"""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(DB_URL, row_factory=dict_row) as conn:
        # 1. 找到最新的检查点
        cp = conn.execute('''
            SELECT checkpoint_id, checkpoint->>'ts' as ts,
                   checkpoint->'channel_versions'->>'messages' as messages_version
            FROM checkpoints
            WHERE thread_id = %s
            ORDER BY checkpoint->>'ts' DESC
            LIMIT 1
        ''', (thread_id,)).fetchone()

        if not cp:
            print(f"❌ 未找到 thread_id={thread_id} 的检查点")
            return

        # 2. 获取 messages blob
        blob = conn.execute('''
            SELECT blob, type
            FROM checkpoint_blobs
            WHERE thread_id = %s AND channel = 'messages' AND version = %s
        ''', (thread_id, cp['messages_version'])).fetchone()

        if not blob:
            print(f"❌ 未找到 messages blob")
            return

        # 3. 反序列化
        try:
            from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
            serde = JsonPlusSerializer()
            messages = serde.loads_typed((blob['type'], blob['blob']))
            if not isinstance(messages, list):
                messages = [messages]
        except Exception as e:
            print(f"❌ 反序列化失败: {e}")
            return

        if limit:
            messages = messages[-limit:]

        if output_json:
            print(json.dumps(messages, indent=2, ensure_ascii=False, default=str))
        else:
            print(f"\n{'='*80}")
            print(f"📋 会话聊天历史 (直接查询)")
            print(f"{'='*80}")
            print(f"Thread ID: {thread_id}")
            print(f"Checkpoint Time: {format_time(cp['ts'])}")
            print(f"消息数量: {len(messages)}")
            print(f"{'='*80}\n")

            for i, msg in enumerate(messages, 1):
                print(format_message(msg, i))
                print()

        # 4. 应用 limit
        if limit:
            messages = messages[-limit:]

        # 5. 输出
        if output_json:
            print(json.dumps(messages, indent=2, ensure_ascii=False))
        else:
            print(f"\n{'='*80}")
            print(f"📋 会话聊天历史: {thread_id}")
            print(f"{'='*80}")
            print(f"检查点时间: {format_time(cp['ts'])}")
            print(f"消息数量: {len(messages)}")
            print(f"{'='*80}\n")

            for i, msg in enumerate(messages):
                # 处理 LangChain 消息对象
                if hasattr(msg, 'type'):
                    msg_type = msg.type
                elif isinstance(msg, dict):
                    msg_type = msg.get('type', 'unknown')
                else:
                    msg_type = 'unknown'

                # 获取内容
                if hasattr(msg, 'content'):
                    content = msg.content
                elif isinstance(msg, dict):
                    content = msg.get('content', '')
                else:
                    content = str(msg)

                # 提取文本内容
                text = ''
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get('type') == 'text':
                            text += part.get('text', '')
                        elif isinstance(part, str):
                            text += part
                elif isinstance(content, str):
                    text = content

                # 获取工具调用
                tool_calls = []
                if hasattr(msg, 'tool_calls'):
                    tool_calls = msg.tool_calls or []
                elif isinstance(msg, dict):
                    tool_calls = msg.get('tool_calls', [])

                # 格式化输出
                if msg_type == 'human':
                    print(f"👤 用户 [{i+1}]:")
                    print(f"   {text[:200]}{'...' if len(text) > 200 else ''}")
                elif msg_type == 'ai':
                    if tool_calls:
                        print(f"🤖 助手 [{i+1}]: (调用工具)")
                        for tc in tool_calls:
                            if hasattr(tc, 'name'):
                                print(f"   📞 {tc.name}({json.dumps(tc.args, ensure_ascii=False)[:100]})")
                            elif isinstance(tc, dict):
                                print(f"   📞 {tc.get('name', 'unknown')}({json.dumps(tc.get('args', {}), ensure_ascii=False)[:100]})")
                    else:
                        print(f"🤖 助手 [{i+1}]:")
                        print(f"   {text[:200]}{'...' if len(text) > 200 else ''}")
                elif msg_type == 'tool':
                    tool_name = ''
                    if hasattr(msg, 'name'):
                        tool_name = msg.name
                    elif isinstance(msg, dict):
                        tool_name = msg.get('name', 'unknown')
                    print(f"🔧 工具 [{i+1}]: {tool_name}")
                    print(f"   {text[:150]}{'...' if len(text) > 150 else ''}")
                else:
                    print(f"❓ {msg_type} [{i+1}]:")
                    print(f"   {text[:150]}{'...' if len(text) > 150 else ''}")

                print()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="获取聊天历史记录")
    parser.add_argument("thread_id", help="Thread ID")
    parser.add_argument("--limit", type=int, help="只显示最近 N 条消息")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式")
    parser.add_argument("--direct", action="store_true", help="直接查询数据库 (不使用 LangGraph API)")

    args = parser.parse_args()

    if args.direct:
        get_chat_history_direct(args.thread_id, args.limit, args.json)
    else:
        asyncio.run(get_chat_history_api(args.thread_id, args.limit, args.json))


if __name__ == "__main__":
    main()
