"""测试 PostgreSQL 存储和 Agent 初始化。"""
import asyncio
import sys
import uuid
from pathlib import Path

# 以仓库根目录为基准推导 src/，不依赖任何本机绝对路径
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from api_view.agent_loader import agent_loader


async def test_pg_operations():
    """测试 PostgreSQL 展示消息存取"""
    print("\n=== 测试 1: PostgreSQL 展示消息存取 ===")
    thread_id = f"test-{uuid.uuid4()}"

    # 保存消息
    messages = [
        {"id": "1", "role": "user", "content": "你好"},
        {"id": "2", "role": "assistant", "content": "你好！我是擎驰采购智能助手"},
    ]
    ok = await agent_loader.save_display_messages(thread_id, messages)
    print(f"保存消息: {'OK' if ok else 'FAIL'}")

    # 读取消息
    loaded = await agent_loader.get_display_messages(thread_id)
    print(f"读取消息数量: {len(loaded) if loaded else 0}")
    if loaded:
        print(f"第一条: {loaded[0]['content']}")

    # 获取所有 thread_id
    ids = agent_loader.get_all_thread_ids()
    print(f"所有会话数: {len(ids)}")

    # 获取更新时间
    t = agent_loader.get_session_updated_at(thread_id)
    print(f"更新时间: {t}")

    # 删除会话
    deleted = await agent_loader.delete_session(thread_id)
    print(f"删除会话: {'OK' if deleted else 'FAIL'}")

    # 验证已删除
    loaded2 = await agent_loader.get_display_messages(thread_id)
    print(f"删除后读取: {'None(正确)' if loaded2 is None else 'FAIL(未删除)'}")


async def test_agent_init():
    """测试 Agent 初始化（会创建沙箱，较慢）"""
    print("\n=== 测试 2: Agent 初始化 ===")
    try:
        agent = await agent_loader.initialize()
        print(f"Agent 初始化成功: {type(agent).__name__}")
        return agent
    except Exception as e:
        print(f"Agent 初始化失败: {e}")
        import traceback
        traceback.print_exc()
        return None


async def test_chat(agent):
    """测试简单对话"""
    if agent is None:
        print("跳过对话测试（Agent 未初始化）")
        return
    print("\n=== 测试 3: 简单对话 ===")
    thread_id = f"chat-{uuid.uuid4()}"
    config = agent_loader.create_config(thread_id)
    try:
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": "你好，请用一句话介绍你自己"}]},
            config=config,
            context={"user_id": "test", "username": "test"},
        )
        msgs = result.get("messages", [])
        if msgs:
            last = msgs[-1]
            content = getattr(last, "content", str(last))
            print(f"AI 回复: {str(content)[:200]}")
            print("对话测试: OK")
        else:
            print("对话测试: 无返回消息")
    except Exception as e:
        print(f"对话测试失败: {e}")
        import traceback
        traceback.print_exc()


async def main():
    await test_pg_operations()
    agent = await test_agent_init()
    await test_chat(agent)
    print("\n=== 全部测试完成 ===")


if __name__ == "__main__":
    asyncio.run(main())
