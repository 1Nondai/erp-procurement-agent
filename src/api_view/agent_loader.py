"""
Agent 加载器

使用单例模式加载和管理 DeepAgent 实例。
Agent 实例通过 agent.main_agent.get_agent_async() 懒加载，
内部已集成 PostgresSaver checkpointer 实现对话状态持久化。
"""

import json
import sys
from pathlib import Path
from typing import Optional, List, Dict, Any
import uuid
from datetime import datetime

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

# 将 src/ 加入 sys.path：api_view / agent / mcp_server 都直挂在 src 下，
# 这样即使没有预设 PYTHONPATH，作为脚本直接运行时也能 import 到兄弟包。
SRC_DIR = Path(__file__).resolve().parent.parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# 导入配置（必须在 sys.path 调整之后）
from api_view.web_config import POSTGRES_URI  # noqa: E402

# 会话归属的兜底用户：历史数据（user_id 为 NULL 的行）按此用户处理，
# 这样升级到多用户后，旧的单人会话不会"凭空消失"或对所有人可见。
from agent.config import DEFAULT_USER_ID  # noqa: E402


# ============================================================
# 性能修复：异步连接池（原实现为每次操作同步新建连接）
# ============================================================
# 原实现的三个问题：
#   1. get_pg_connection() 每次调用都 psycopg.connect() 新建 TCP 连接
#      （psycopg3 默认没有池化——原注释"内部有连接池优化"是不准确的），
#      每次读写消息都要付一次 3 次握手 + 认证的开销；
#   2. save/get_display_messages 声明为 async 却在内部跑同步 SQL，
#      会阻塞 FastAPI 的事件循环——SSE 流式输出期间一卡，所有并发用户都卡；
#   3. _ensure_tables() 在每个读写方法里都执行 5 条 DDL，浪费且无必要。
#
# 新方案：
#   - AsyncConnectionPool：进程级单例，后台维护连接，事件循环友好；
#   - open=True 幂等（已开则跳过），min_size 小（本应用并发低）；
#   - _ensure_tables 只在池首次创建时执行一次（DDL 本身幂等，重复执行无意义）。
#
# 为什么用模块级懒加载而不是 lifespan 里创建：
#   本模块被 chat.py / history.py / start_web.py 多处 import，
#   模块级 _get_pool() 保证任何调用路径拿到的都是同一个池；
#   池的创建放在首次使用时而非 import 时，避免"只是 import 就连数据库"。
_POOL: Optional[AsyncConnectionPool] = None
_TABLES_READY = False


async def get_pg_pool() -> AsyncConnectionPool:
    """获取（并按需创建）全局异步连接池单例。

    首次调用时创建池，并在建表未完成时触发一次 _ensure_tables_async()；
    之后所有异步数据库操作复用该池。

    职责边界（P0 修复）：本函数只负责"建池 + 触发建表"，
    不再负责把 _TABLES_READY 置位——置位由 _ensure_tables_async 自己完成。
    原因：原实现里本函数在 _TABLES_READY 为 False 时 await 建表，
    而建表函数内部又 await 本函数取连接，两者互相 await，
    后端首次访问数据库必定 RecursionError（见 _ensure_tables_async 的说明）。

    Returns:
        AsyncConnectionPool: psycopg3 异步连接池实例。
    """
    global _POOL
    if _POOL is None:
        _POOL = AsyncConnectionPool(
            POSTGRES_URI,
            min_size=1,
            max_size=8,
            open=True,          # 立即打开（幂等；已打开的池不会重复初始化）
            kwargs={"row_factory": dict_row},
        )
    # 建表是幂等 DDL，_TABLES_READY 为 True 后不再重复执行
    if not _TABLES_READY:
        await _ensure_tables_async()
    return _POOL


async def _ensure_tables_async() -> None:
    """创建展示消息表（幂等，进程生命周期内只执行一次）。

    为什么这里直接用模块级 _POOL、而不调用 get_pg_pool() 取连接：
      get_pg_pool() 在 _TABLES_READY 为 False 时会 await 本函数，
      若本函数再 await get_pg_pool() 就立刻回到上一步 —— 两者无限互递归，
      第一次访问数据库必然 RecursionError（栈溢出，而不是连不上库）。
      因此本函数只使用"已经建好的池"：池为 None 说明调用顺序错了
      （有人绕过 get_pg_pool 直接建表），此时显式抛 RuntimeError 比递归崩栈可诊断得多。
      建表完成后再由本函数自己置 _TABLES_READY=True：谁完成谁置位，语义单一。

    P0 变更：新增 user_id 列用于会话归属。
      - 新库直接建在 CREATE TABLE 里；
      - 已有库走 ALTER TABLE ... ADD COLUMN IF NOT EXISTS，
        升级无需手工迁移脚本，也不会丢历史数据；
      - 历史行的 user_id 为 NULL，查询时用 COALESCE 归入 DEFAULT_USER_ID。
    """
    global _TABLES_READY
    pool = _POOL
    if pool is None:
        raise RuntimeError(
            "连接池尚未初始化：_ensure_tables_async 必须由 get_pg_pool() 触发，不能单独调用"
        )
    async with pool.connection() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS session_display_messages (
                id SERIAL PRIMARY KEY,
                thread_id TEXT NOT NULL,
                index INTEGER NOT NULL,
                message JSONB NOT NULL,
                user_id TEXT,
                updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE(thread_id, index)
            )
        """)
        await conn.execute("""
            ALTER TABLE session_display_messages
            ADD COLUMN IF NOT EXISTS user_id TEXT
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_display_thread
            ON session_display_messages(thread_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_display_user
            ON session_display_messages(user_id)
        """)
        await conn.commit()
    # 建表成功后才置位（提交之后再置位：DDL 失败时保持 False，下次调用会重试）
    _TABLES_READY = True


class AgentLoader:
    """
    Agent 加载器单例类

    负责管理 Agent 生命周期、PostgreSQL 连接和会话相关操作。
    Agent 实例通过 main_agent.get_agent_async() 懒加载，
    checkpointer 由 main_agent 内部统一管理（PostgresSaver）。
    """

    # 类级别的单例实例
    _instance: Optional['AgentLoader'] = None
    # 是否已初始化
    _initialized: bool = False
    # Agent 实例
    _agent = None

    def __new__(cls):
        """确保单例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    async def initialize(self):
        """
        初始化并懒加载 Agent 实例。

        Agent 通过 main_agent.get_agent_async() 获取，
        内部已集成 PostgresSaver checkpointer，无需在此重复创建。

        Returns:
            Agent 实例
        """
        if self._initialized and self._agent is not None:
            return self._agent

        print("[AgentLoader] 开始初始化...")

        try:
            # 确保展示消息表存在（进程首次使用连接池时建表，幂等且只执行一次）
            await get_pg_pool()

            # 通过 main_agent 的懒加载方法获取 Agent（内部已集成 PostgresSaver checkpointer）
            from agent.main_agent import get_agent_async
            self._agent = await get_agent_async()

            self._initialized = True
            print("[AgentLoader] 初始化完成")
        except Exception:
            raise

        return self._agent

    @staticmethod
    def _ensure_tables():
        """[已废弃] 同步建表。保留为空实现以兼容旧引用；实际建表由 _ensure_tables_async 完成。"""
        return

    @property
    def agent(self):
        """
        获取 Agent 实例

        Returns:
            Agent 实例

        Raises:
            RuntimeError: 如果 Agent 未初始化
        """
        if self._agent is None:
            raise RuntimeError("Agent 未初始化，请先调用 initialize() 方法")
        return self._agent

    def create_config(
        self,
        thread_id: Optional[str] = None,
        user_id: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        创建 Agent 调用配置

        Args:
            thread_id: 会话 ID，为空则自动生成
            user_id: 用户 ID，用于记忆隔离和上下文注入。
                     为空时回退到 config.DEFAULT_USER_ID（本地开发/脚本调用场景）。
            **kwargs: 其他可配置的参数

        Returns:
            Dict: Agent 配置字典

        说明：
          - configurable.user_id 会被运行时上下文（ProcurementContext）读取，
            进而决定 /memories/{user_id}/ 的读写位置与 CompositeBackend 的 namespace；
          - metadata.user_id 便于 LangSmith/Langfuse 之类的追踪里按用户过滤，
            属于可观测性用途，不参与业务逻辑。
        """
        resolved_thread_id = thread_id or str(uuid.uuid4())
        resolved_user_id = user_id or DEFAULT_USER_ID
        return {
            "configurable": {
                "thread_id": resolved_thread_id,
                "user_id": resolved_user_id,
                **kwargs
            },
            "metadata": {
                "thread_id": resolved_thread_id,
                "user_id": resolved_user_id,
            },
        }

    async def get_state_history(
        self,
        thread_id: str,
        limit: int = 50
    ) -> List[Any]:
        """
        获取会话的历史状态

        Args:
            thread_id: 会话 ID
            limit: 返回的最大状态数量

        Returns:
            List[StateSnapshot]: 状态快照列表
        """
        config = self.create_config(thread_id)
        states = []

        # 使用 agent.get_state_history 获取历史
        async for state in self.agent.aget_state_history(config):
            states.append(state)
            if len(states) >= limit:
                break

        return states

    async def get_current_messages(self, thread_id: str) -> List[Dict[str, Any]]:
        """
        获取当前会话的消息列表

        Args:
            thread_id: 会话 ID

        Returns:
            List[Dict]: 消息列表
        """
        config = self.create_config(thread_id)

        try:
            state = await self.agent.aget_state(config)
            if state and state.values:
                return state.values.get("messages", [])
        except Exception as e:
            print(f"[AgentLoader] 获取消息失败: {e}")

        return []

    def get_all_thread_ids(self, user_id: Optional[str] = None) -> List[str]:
        """[保留同步] 获取会话 thread_id 列表。

        之所以保留同步实现：调用方（chat.py / history.py）在 async 上下文中调用，
        但列表查询是低频操作；且 check_thread_access 依赖它做越权校验，
        保持同步可以避免在鉴权路径上引入 await 传染。
        代价：查询期间会短暂阻塞事件循环，因此本方法走独立短连接并限制结果规模。

        Args:
            user_id: 传入时只返回该用户的会话（多用户隔离）；
                     为 None 时返回全部（脚本/运维用途）。

        为什么不用 checkpoints 表过滤用户：
          LangGraph 的 checkpoints 表没有业务侧用户字段，硬塞会侵入框架表结构。
          展示消息表（session_display_messages）由本项目维护且已带 user_id，
          因此以它为准；历史行的 user_id 为 NULL，用 COALESCE 归入默认用户。
        """
        try:
            with psycopg.connect(POSTGRES_URI, row_factory=dict_row) as conn:
                if user_id:
                    result = conn.execute(
                        """
                        SELECT DISTINCT thread_id FROM session_display_messages
                        WHERE COALESCE(user_id, %s) = %s AND thread_id IS NOT NULL
                        """,
                        (DEFAULT_USER_ID, user_id),
                    ).fetchall()
                else:
                    # 无用户过滤时以 checkpoint 表为全集，避免"只有展示消息没有 checkpoint"的脏数据
                    result = conn.execute(
                        "SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id IS NOT NULL"
                    ).fetchall()
                thread_ids = [row["thread_id"] for row in result]
                # 过滤掉空值和通配符
                return [tid for tid in thread_ids if tid and tid != "*" and tid != ""]
        except Exception as e:
            print(f"[AgentLoader] 获取会话列表失败: {e}")
            return []

    def get_thread_owner(self, thread_id: str) -> Optional[str]:
        """查询会话归属的 user_id（历史数据返回默认用户）。同步实现，理由同上。"""
        try:
            with psycopg.connect(POSTGRES_URI, row_factory=dict_row) as conn:
                row = conn.execute(
                    """
                    SELECT COALESCE(MAX(user_id), %s) AS owner
                    FROM session_display_messages WHERE thread_id = %s
                    """,
                    (DEFAULT_USER_ID, thread_id),
                ).fetchone()
                return row["owner"] if row else None
        except Exception as e:
            print(f"[AgentLoader] 查询会话归属失败: {e}")
            return None

    def check_thread_access(self, thread_id: str, user_id: Optional[str]) -> bool:
        """校验会话是否属于该用户（越权防护）。

        规则：user_id 为 None（内部调用）时放行；否则必须与会话归属一致。
        读和写都要走这一步——否则换个 thread_id 就能读别人的对话与记忆。
        """
        if not user_id:
            return True
        owner = self.get_thread_owner(thread_id)
        if owner is None:
            return True          # 会话不存在（或还没有展示消息），交由后续逻辑处理
        if owner != user_id:
            print(f"[AgentLoader] 拒绝越权访问: thread={thread_id} owner={owner} request={user_id}")
            return False
        return True

    def get_session_updated_at(self, thread_id: str) -> datetime:
        """获取会话最后更新时间（同步短连接，低频操作）。"""
        try:
            with psycopg.connect(POSTGRES_URI, row_factory=dict_row) as conn:
                result = conn.execute(
                    "SELECT MAX(updated_at) as max_time FROM session_display_messages WHERE thread_id = %s",
                    (thread_id,)
                ).fetchone()
                if result and result["max_time"]:
                    return result["max_time"]
        except Exception as e:
            print(f"[AgentLoader] 获取会话时间失败: {e}")
        return datetime.now()

    async def delete_session(self, thread_id: str, user_id: Optional[str] = None) -> bool:
        """
        删除会话（删除该 thread_id 下的所有 checkpoint 和展示消息）

        Args:
            thread_id: 会话 ID
            user_id: 请求方用户；传入时校验归属，越权直接拒绝

        Returns:
            bool: 是否删除成功
        """
        try:
            if not self.check_thread_access(thread_id, user_id):
                return False
            pool = await get_pg_pool()
            async with pool.connection() as conn:
                # 删除该 thread_id 的所有展示消息
                result = await conn.execute(
                    "DELETE FROM session_display_messages WHERE thread_id = %s",
                    (thread_id,)
                )
                display_count = result.rowcount

                # 删除 checkpoint 表中的记录
                cp_result = await conn.execute(
                    "DELETE FROM checkpoints WHERE thread_id = %s",
                    (thread_id,)
                )
                cp_count = cp_result.rowcount

                # 同时删除 checkpoint_blobs 和 checkpoint_writes
                await conn.execute(
                    "DELETE FROM checkpoint_blobs WHERE thread_id = %s",
                    (thread_id,)
                )
                await conn.execute(
                    "DELETE FROM checkpoint_writes WHERE thread_id = %s",
                    (thread_id,)
                )
                await conn.commit()

                print(f"[AgentLoader] 已删除会话 {thread_id}，checkpoint {cp_count} 条，展示消息 {display_count} 条")
                return True
        except Exception as e:
            print(f"[AgentLoader] 删除会话失败: {e}")
            import traceback
            traceback.print_exc()
            return False

    # ============================================================
    # 完整展示消息存取（包含子代理消息，解决 checkpoint 丢失子代理消息的问题）
    # ============================================================

    # 单字段最大字符数（约 500KB），防止单条消息过大
    _MAX_FIELD_LENGTH = 500_000

    @classmethod
    def _truncate_message_fields(cls, msg: Dict[str, Any]) -> Dict[str, Any]:
        """截断消息中的超长文本字段"""
        for field in ("text", "content", "args"):
            if field in msg and isinstance(msg[field], str) and len(msg[field]) > cls._MAX_FIELD_LENGTH:
                msg[field] = msg[field][:cls._MAX_FIELD_LENGTH] + "\n\n...(内容过长已截断)"
        return msg

    async def save_display_messages(
        self, thread_id: str, messages: List[Dict[str, Any]], user_id: Optional[str] = None
    ) -> bool:
        """
        将完整的展示消息批量存入 PostgreSQL（异步连接池）

        Args:
            thread_id: 会话 ID
            messages: 完整的展示消息列表
            user_id: 会话归属用户（写入 user_id 列，供列表过滤与越权校验使用）

        Returns:
            bool: 是否保存成功

        写入策略（相比原实现的三点优化）：
          1. 异步连接池：原实现在 async 函数里跑同步 psycopg，
             SSE 流式期间会阻塞整个事件循环（所有并发用户一起卡顿）；
          2. executemany 批量插入：原来逐条 INSERT，N 条消息 = N 次网络往返，
             现在一次批量提交；
          3. 仍是"先删后插"的全量覆盖（消息序号即顺序），语义不变：
             resume 场景传入的 messages 必须是"历史 + 本轮"的完整列表。
        """
        try:
            pool = await get_pg_pool()
            async with pool.connection() as conn:
                # 删除该线程的旧消息
                await conn.execute(
                    "DELETE FROM session_display_messages WHERE thread_id = %s",
                    (thread_id,)
                )

                # 批量插入（一次网络往返）
                if messages:
                    now = datetime.now()
                    rows = [
                        (
                            thread_id, i,
                            json.dumps(self._truncate_message_fields(msg), ensure_ascii=False, default=str),
                            user_id, now,
                        )
                        for i, msg in enumerate(messages)
                    ]
                    await conn.executemany(
                        """
                        INSERT INTO session_display_messages (thread_id, index, message, user_id, updated_at)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        rows,
                    )
                await conn.commit()
                print(f"[AgentLoader] 已保存 {len(messages)} 条展示消息，thread_id={thread_id}, user_id={user_id}")

            return True
        except Exception as e:
            print(f"[AgentLoader] 保存展示消息失败: {e}")
            import traceback
            traceback.print_exc()
            return False

    async def get_display_messages(
        self, thread_id: str, user_id: Optional[str] = None
    ) -> Optional[List[Dict[str, Any]]]:
        """
        从 PostgreSQL 读取完整的展示消息（异步连接池）

        Args:
            thread_id: 会话 ID
            user_id: 请求方用户；传入时先校验归属（越权返回 None，等价于"看不到"）

        Returns:
            List[Dict] 或 None: 完整的展示消息列表，不存在时返回 None
        """
        try:
            if not self.check_thread_access(thread_id, user_id):
                return None
            pool = await get_pg_pool()
            async with pool.connection() as conn:
                result = await conn.execute(
                    "SELECT message FROM session_display_messages WHERE thread_id = %s ORDER BY index ASC",
                    (thread_id,)
                )
                rows = await result.fetchall()

                if not rows:
                    return None

                messages = []
                for row in rows:
                    msg = row["message"]
                    # JSONB 字段 psycopg 自动解析为 dict，无需 json.loads
                    if isinstance(msg, str):
                        msg = json.loads(msg)
                    messages.append(msg)
                print(f"[AgentLoader] 已读取 {len(messages)} 条展示消息，thread_id={thread_id}")
                return messages
        except Exception as e:
            print(f"[AgentLoader] 读取展示消息失败: {e}")
            import traceback
            traceback.print_exc()
            return None


# 全局单例实例
agent_loader = AgentLoader()
