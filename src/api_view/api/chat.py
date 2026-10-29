"""
对话 API 模块

提供流式对话接口、中断恢复接口和会话状态查询接口
"""

import json
import uuid
import re
import os
import tempfile
from datetime import datetime
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from langgraph.types import Command
from pydantic import BaseModel

from agent.schema import (
    ChatRequest,
    ChatResponse,
    Message,
)
from api_view.agent_loader import agent_loader
from api_view.identity import resolve_identity
# 复用 history.py 里的消息读取辅助函数：兼容 LC 消息对象，避免重复实现
from api_view.api.history import get_message_attr, get_message_content, get_message_role


# 创建路由
router = APIRouter()

# 调试日志文件路径（在系统临时目录下，避免污染项目目录）
DEBUG_LOG_DIR = os.path.join(tempfile.gettempdir(), "deepagent_debug")

# ---- 性能修复：per-token 调试日志改为"环境变量开关 + 内存缓冲" ----
# 原实现的问题：流式响应期间，每收到一个 token（RAW_CHUNK）就执行一次
# open() + write() + close() 的磁盘 IO。一次对话几百上千个 token，
# 意味着几百上千次文件打开/关闭，既拖慢打字速度，也加速磁盘损耗。
#
# 新方案（三层过滤）：
#   1. DEBUG_STREAM=1 才启用调试日志，默认关闭（生产零开销）；
#   2. RAW_CHUNK 级别（每个 token 一条）默认不记录，除非 DEBUG_STREAM=2；
#      TOKEN / TOOL 级别的事件在 DEBUG_STREAM=1 时仍会记录——
#      排查"工具没被调用"问题时 1 级已够用，2 级专用于查流式细节；
#   3. 写文件复用 logging（同步写但频率低）+ 每条日志截断，避免大 JSON。
STREAM_DEBUG_LEVEL = int(os.environ.get("DEBUG_STREAM", "0"))


def get_debug_log_path(thread_id: str) -> str:
    """获取当前会话的调试日志文件路径。

    thread_id 做字符消毒（去掉路径分隔符并截断），
    防止调试日志路径被会话 ID 注入成任意路径。
    """
    os.makedirs(DEBUG_LOG_DIR, exist_ok=True)
    safe_id = thread_id.replace("/", "_").replace("\\", "_")[:50]
    return os.path.join(DEBUG_LOG_DIR, f"stream_{safe_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")


def write_debug_log(filepath: str, event_type: str, data: dict, raw_token: object = None):
    """写入调试日志（仅当环境变量启用时）。

    设计要点：
      - DEBUG_STREAM 未设置时函数直接返回——一次字符串比较的开销，
        换掉原来每个 token 的 open/close 磁盘 IO；
      - raw_token 只提取关键字段并截断，防止整个消息对象被序列化进日志。
    """
    if STREAM_DEBUG_LEVEL <= 0:
        return
    # RAW_CHUNK 是最高频事件（每个 token 一条），只在 2 级调试时记录
    if event_type == "RAW_CHUNK" and STREAM_DEBUG_LEVEL < 2:
        return
    try:
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat()}] {event_type}\n")
            f.write(f"  data: {json.dumps(data, ensure_ascii=False, default=str)[:2000]}\n")
            if raw_token is not None:
                token_info = {}
                if hasattr(raw_token, 'type'):
                    token_info['type'] = raw_token.type
                if hasattr(raw_token, 'name'):
                    token_info['name'] = raw_token.name
                if hasattr(raw_token, 'content'):
                    c = raw_token.content
                    if isinstance(c, str):
                        token_info['content_preview'] = c[:500]
                    elif isinstance(c, list):
                        token_info['content_len'] = len(c)
                        token_info['content_preview'] = str(c)[:500]
                    else:
                        token_info['content_preview'] = str(c)[:500]
                if hasattr(raw_token, 'tool_call_chunks') and raw_token.tool_call_chunks:
                    token_info['tool_call_chunks'] = str(raw_token.tool_call_chunks)[:500]
                if hasattr(raw_token, 'id'):
                    token_info['id'] = raw_token.id
                f.write(f"  token: {json.dumps(token_info, ensure_ascii=False, default=str)[:2000]}\n")
            f.write("\n")
    except Exception:
        pass  # 调试日志失败不影响主流程


def extract_subagent_name(namespace: tuple) -> str:
    """
    从 namespace 中提取子代理名称
    """
    for segment in namespace:
        if segment.startswith("tools:"):
            return segment.replace("tools:", "")
    return "main"


def is_likely_uuid(text: str) -> bool:
    """
    判断文本是否大概率是 UUID（而非有意义的回复内容）
    例如：3576bba4-42e5-a769-c2f7-ea8444829951
    """
    # UUID 格式：8-4-4-4-12
    uuid_pattern = r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
    return bool(re.match(uuid_pattern, text.strip()))


def extract_content_from_token(token) -> str:
    """
    从 token 中提取文本内容

    处理 token.content 可能是字符串、列表或其他类型的情况
    过滤掉明显是 UUID 或无关标识符的内容
    """
    if not hasattr(token, 'content'):
        return ""

    content = token.content

    # 如果 content 是字符串，直接返回
    if isinstance(content, str):
        # 过滤纯 UUID 内容
        if is_likely_uuid(content):
            return ""
        return content

    # 如果 content 是列表，尝试提取其中的文本
    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, str):
                text_parts.append(item)
            elif isinstance(item, dict):
                # 结构化内容：只提取 text 类型，忽略 image_url 等
                item_type = item.get("type", "")
                if item_type == "text":
                    t = item.get("text", "")
                    if not is_likely_uuid(t):
                        text_parts.append(t)
                elif item_type == "image_url":
                    # 图片 URL 不提取为文本，由 serialize_tool_result 处理
                    pass
                elif "text" in item:
                    t = item["text"]
                    if not is_likely_uuid(str(t)):
                        text_parts.append(str(t))
                elif "content" in item:
                    text_parts.append(str(item["content"]))
            else:
                text_parts.append(str(item))
        return ''.join(text_parts)

    # 其他类型转换为字符串
    return str(content) if content is not None else ""


def serialize_tool_result(content) -> dict:
    """
    将工具执行结果序列化为前端可用的结构化数据

    处理 tool 消息的 content，可能是字符串、列表（包含文本和图片）
    提取文本和图片数据，返回结构化字典
    """
    result = {"text": "", "images": []}

    if isinstance(content, str):
        result["text"] = content
        # 提取 markdown 图片语法
        for match in re.finditer(r'!\[.*?\]\((.*?)\)', content):
            url = match.group(1)
            if url not in result["images"]:
                result["images"].append(url)

    elif isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, str):
                text_parts.append(item)
            elif isinstance(item, dict):
                item_type = item.get("type", "")
                if item_type == "text":
                    t = item.get("text", "")
                    text_parts.append(t)
                elif item_type == "image_url":
                    image_url = item.get("image_url", {})
                    url = image_url.get("url", "") if isinstance(image_url, dict) else str(image_url)
                    if url:
                        result["images"].append(url)
                elif item_type == "image":
                    image_data = item.get("data", "") or item.get("image_data", "")
                    if image_data:
                        result["images"].append(image_data)
                elif "text" in item:
                    text_parts.append(item["text"])
                elif "content" in item:
                    text_parts.append(str(item["content"]))
                else:
                    text_parts.append(str(item))
            else:
                text_parts.append(str(item))
        result["text"] = ''.join(text_parts)
    else:
        result["text"] = str(content) if content is not None else ""

    # 从文本中提取所有图片 URL（包括 markdown 语法和直链）
    # 支持常见图片托管域名（即使 URL 不以图片扩展名结尾）
    if result["text"]:
        for match in re.finditer(r'!\[.*?\]\((.*?)\)', result["text"]):
            url = match.group(1)
            if url not in result["images"]:
                result["images"].append(url)
        # 也检测 data:image base64
        for match in re.finditer(r'data:image/[^;]+;base64,[A-Za-z0-9+/=]+', result["text"]):
            url = match.group(0)
            if url not in result["images"]:
                result["images"].append(url)

    return result


def create_sse_message(data: dict) -> str:
    """创建 SSE 格式的消息"""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


# ============================================================
# 中断恢复请求模型
# ============================================================

class ResumeRequest(BaseModel):
    """中断恢复请求体，resume 字段的格式取决于中断类型：
    - 数据补充中断: {"supplement": "用户自由文本输入"}
    - HITL 审批中断: {"decisions": [{"type": "approve"}]} 或 [{"type": "reject"}]
    """
    resume: dict


# ============================================================
# 流式对话核心逻辑
# ============================================================

async def _has_pending_interrupt(thread_id: str, config: dict) -> bool:
    """判断该会话是否真的存在待处理中断。

    为什么需要它：resume 接口对调用方是幂等的"继续执行"，但底层会真的跑副作用
    （下单/改单）。用户连点两次审批、或前端重试，就会把写操作执行两遍。
    这里用 LangGraph 的状态快照做硬判据 —— StateSnapshot.tasks[*].interrupts 非空
    才说明图停在中断点等待输入；否则说明中断已被消费，本次 resume 应当忽略。

    失败策略（P0 修复）：查询异常时返回 False —— fail-closed。
    原实现返回 True 是 fail-open，"查不到就放行"恰好把防护能力放到了最需要它的
    场景之外：checkpointer 抖动 / 状态读超时的时候，重复点审批照样执行第二遍写操作。
    改成 False 的语义是"无法证明有待处理中断，就不恢复"，用户最多刷新重试一次，
    不会多下一次单。该函数只服务于带副作用的 resume 路径；
    读接口（GET /chat/{thread_id}、/history/*）不经过它，行为不变。
    """
    try:
        state = await agent_loader.agent.aget_state(config)
    except Exception as e:  # noqa: BLE001
        # 判据不可用：拒绝本次恢复（fail-closed），并记录以便排查
        write_debug_log(
            os.path.join(DEBUG_LOG_DIR, "resume_guard.log"),
            "PENDING_CHECK_FAILED",
            {"thread_id": thread_id, "error": str(e)},
        )
        return False

    for task in (getattr(state, "tasks", None) or ()):
        if getattr(task, "interrupts", None):
            return True
    return False


async def stream_chat_response(
    message: str = None,
    thread_id: str = None,
    resume_data: dict = None,
    user_id: str = None,
    username: str = None,
) -> AsyncIterator[str]:
    """
    流式生成对话响应，支持 Human-in-the-Loop 中断与恢复。

    两种调用模式：
    1. 初始对话 — 传入 message（用户消息）
    2. 中断恢复 — 传入 resume_data（Command.resume 的值）

    当 Agent 触发中断时（request_order_info 数据补充 / order_create|update HITL 审批），
    流会发送 interrupt 事件后结束。前端收集用户决策后通过 /resume 端点恢复。

    同时累积完整的展示消息列表（包含子代理消息），在流结束后存入 PostgreSQL。

    Args:
        user_id: 已由 identity.resolve_identity() 校验过的用户 ID；
                 决定记忆命名空间（/memories/{user_id}/）与会话归属。
        username: 展示名，供上下文注入使用。
    """
    # 身份来自请求头（api_view/identity.py 已做白名单校验与兜底），不再硬编码
    context = {"user_id": user_id, "username": username or user_id}
    config = agent_loader.create_config(thread_id, user_id=user_id)
    collected_content = ""
    # 工具调用登记表：tool_call_id → 工具信息 dict。
    # P1 修复：原实现用"栈 + 盲弹栈顶"配对工具开始/结束——
    # LangGraph 允许同一轮并行发起多个工具调用（AIMessage.tool_calls 是列表），
    # 完成顺序不保证 LIFO：A 先发起、B 先完成时，B 的结果会错误地配到 A 的卡片上。
    # 改为按 tool_call_id 精确配对（嵌套场景下 id 天然不同，不受影响）。
    tool_call_registry: dict = {}

    # ---- 越权校验：换个 thread_id 就能读/写别人会话，是必须拦住的一步 ----
    if not agent_loader.check_thread_access(thread_id, user_id):
        yield create_sse_message({"type": "error", "message": "无权访问该会话"})
        return

    # ---- 根据模式构建 input 和 display_messages ----
    if resume_data is not None:
        # 恢复模式：先确认确实有待处理中断（重放保护 + 写操作防重复执行）
        # 注意：判据查询失败时 _has_pending_interrupt 返回 False（fail-closed），
        # 与"中断已被消费"走同一条拒绝分支，因此提示语要同时覆盖这两种情况，
        # 并引导用户刷新重试（写操作宁可少执行一次，也不能重复执行）。
        if not await _has_pending_interrupt(thread_id, config):
            yield create_sse_message({
                "type": "error",
                "message": "无法确认该会话存在待处理的中断（可能已处理过，或状态查询失败），"
                           "本次恢复请求已忽略；若仍在等待审批，请刷新页面后重试",
            })
            return
        existing = await agent_loader.get_display_messages(thread_id, user_id=user_id) or []
        display_messages = existing
        current_input = Command(resume=resume_data)
    else:
        # 初始模式：新建展示消息，用普通消息作为 input
        current_input = {"messages": [{"role": "user", "content": message}]}
        display_messages = [
            {
                "id": f"user-{uuid.uuid4()}",
                "role": "user",
                "content": message
            }
        ]

    # 创建调试日志文件
    debug_log = get_debug_log_path(thread_id)

    def _last_display_is_assistant():
        return (display_messages and
                display_messages[-1]["role"] == "assistant")

    try:
        write_debug_log(debug_log, "STREAM_START", {
            "message": message,
            "thread_id": thread_id,
            "is_resume": resume_data is not None,
        })

        # 流式调用 agent.astream()
        # stream_mode=["messages", "values"] — messages 流显示 + values 流检测中断
        async for chunk in agent_loader.agent.astream(
            input=current_input,
            config=config,
            context=context,
            stream_mode=["messages", "values"],
            subgraphs=True, # 启用子代理流式输出
            version="v2",
        ):
            chunk_type = chunk.get("type")

            # ---- 值流（中断检测，必须在 messages 处理之前）----
            if chunk_type == "values" and chunk.get("interrupts"):
                interrupts = chunk["interrupts"]
                write_debug_log(debug_log, "INTERRUPT_DETECTED", {
                    "count": len(interrupts),
                })

                for interrupt in interrupts:
                    interrupt_value = interrupt.value

                    if "action_requests" in interrupt_value:
                        # ---- 第 2 层：HITL 审批中断（interrupt_on 配置）----
                        yield create_sse_message({
                            "type": "interrupt",
                            "interrupt_type": "hitl_approval",
                            "action_requests": interrupt_value["action_requests"],
                            "review_configs": interrupt_value.get("review_configs", []),
                            "thread_id": thread_id,
                        })
                        write_debug_log(debug_log, "INTERRUPT_HITL", {
                            "actions": [a["name"] for a in interrupt_value["action_requests"]],
                        })

                    elif interrupt_value.get("type") == "order_info_request":
                        # ---- 第 1 层：数据补充中断（request_order_info 工具）----
                        yield create_sse_message({
                            "type": "interrupt",
                            "interrupt_type": "order_info_supplement",
                            "missing_fields": interrupt_value["missing_fields"],
                            "collected_data": interrupt_value["collected_data"],
                            "thread_id": thread_id,
                        })
                        write_debug_log(debug_log, "INTERRUPT_SUPPLEMENT", {
                            "missing_fields": interrupt_value["missing_fields"],
                        })

                    else:
                        # 未知中断类型，透传原始值
                        yield create_sse_message({
                            "type": "interrupt",
                            "interrupt_type": "unknown",
                            "interrupt_value": str(interrupt_value)[:2000],
                            "thread_id": thread_id,
                        })

                # 兜底：将所有 calling 状态的工具标记为 done
                for dm in display_messages:
                    if dm["role"] == "tool" and dm["tool_status"] == "calling":
                        dm["tool_status"] = "done"

                # 清理空的 assistant 消息
                cleaned = [
                    dm for dm in display_messages
                    if not (dm["role"] == "assistant" and not dm.get("content"))
                ]

                # 保存部分展示消息到 PostgreSQL（中断前的消息状态）
                # 注意：resume 模式下 display_messages 已含历史，保存时会覆盖旧记录
                # （这是有意的：覆盖写保证"历史+本轮"始终是完整序列，不会重复追加）
                await agent_loader.save_display_messages(thread_id, cleaned, user_id=user_id)
                write_debug_log(debug_log, "SAVE_DISPLAY_INTERRUPT", {
                    "thread_id": thread_id,
                    "message_count": len(cleaned),
                })

                # 发送 done 事件标记流结束（前端由此知道可以展示中断 UI）
                yield create_sse_message({
                    "type": "done",
                    "thread_id": thread_id,
                    "content": collected_content,
                    "interrupted": True,
                })
                return  # ← 结束本次流，等待前端 POST /resume

            # ---- 消息流（token / tool_call / tool_result）----
            if chunk_type != "messages":
                continue

            token, metadata = chunk["data"]
            namespace = chunk.get("ns", ())

            # 判断消息来源
            is_subagent = any(s.startswith("tools:") for s in namespace)
            source = extract_subagent_name(namespace) if is_subagent else "main"

            write_debug_log(debug_log, "RAW_CHUNK", {
                "ns": list(namespace),
                "source": source,
            }, raw_token=token)

            # 处理工具调用
            if hasattr(token, 'tool_call_chunks') and token.tool_call_chunks:
                for tool_chunk in token.tool_call_chunks:
                    # 工具开始调用
                    if tool_chunk.get('name'):
                        # 优先用 LangChain 生成的 chunk id 作为配对键；
                        # 某些后端 chunk 可能缺 id，此时回退生成一个
                        # （同一 chunk 内 name+index 一致，降级栈顶仍可兜底）。
                        tool_id = str(tool_chunk.get('id') or uuid.uuid4())
                        new_tool = {
                            "id": tool_id,
                            "name": tool_chunk['name'],
                            "args": "",
                            "source": source
                        }
                        tool_call_registry[tool_id] = new_tool

                        yield create_sse_message({
                            "type": "tool_start",
                            "tool_call_id": tool_id,
                            "tool_name": tool_chunk['name'],
                            "source": source
                        })

                        # 添加到展示消息
                        display_messages.append({
                            "id": tool_id,
                            "role": "tool",
                            "tool_name": tool_chunk['name'],
                            "args": "",
                            "text": "",
                            "images": [],
                            "source": source,
                            "tool_status": "calling"
                        })

                        write_debug_log(debug_log, "TOOL_START", {
                            "name": tool_chunk['name'],
                            "source": source,
                            "registry_depth": len(tool_call_registry)
                        })

                    # 工具参数
                    if tool_chunk.get('args'):
                        args_str = tool_chunk['args']
                        # 参数增量归属：优先按 chunk id 定位；缺 id 时退回
                        # "最后一个 calling 的工具"（与旧行为一致，单流式场景正确）
                        target = None
                        chunk_id = tool_chunk.get('id')
                        if chunk_id and str(chunk_id) in tool_call_registry:
                            target = tool_call_registry[str(chunk_id)]
                        elif tool_call_registry:
                            target = list(tool_call_registry.values())[-1]
                        if target is not None:
                            target["args"] += args_str

                        yield create_sse_message({
                            "type": "tool_args",
                            "args": args_str,
                            "source": source
                        })

                        # 更新展示消息中对应工具的 args（先按 id 找，找不到退回最后一个 calling）
                        target_id = str(chunk_id) if chunk_id else None
                        for dm in reversed(display_messages):
                            if dm["role"] == "tool" and dm["tool_status"] == "calling" \
                                    and (target_id is None or dm["id"] == target_id):
                                dm["args"] += args_str
                                break

            # 处理工具执行结果
            if hasattr(token, 'type') and token.type == "tool":
                tool_name = getattr(token, 'name', '未知工具')
                result_content = getattr(token, 'content', '')

                # 序列化工具结果为结构化数据
                serialized = serialize_tool_result(result_content)

                # P1 修复：按 ToolMessage.tool_call_id 精确配对；
                # 找不到登记时回退弹"最后一个 calling"（兼容缺 id 的后端）
                tool_call_id = getattr(token, 'tool_call_id', None)
                finished_tool = tool_call_registry.pop(str(tool_call_id), None) \
                    if tool_call_id else None
                if finished_tool is None and tool_call_registry:
                    finished_tool = list(tool_call_registry.values())[-1]
                    tool_call_registry.pop(finished_tool["id"], None)
                tool_id = finished_tool["id"] if finished_tool else ""

                yield create_sse_message({
                    "type": "tool_result",
                    "tool_name": tool_name,
                    "tool_call_id": tool_id,
                    "text": serialized["text"],
                    "images": serialized["images"],
                    "source": source
                })

                # 更新展示消息中对应工具的结果
                for dm in reversed(display_messages):
                    if dm["role"] == "tool" and dm["id"] == tool_id:
                        dm["text"] = serialized["text"]
                        dm["images"] = serialized["images"]
                        dm["tool_status"] = "done"
                        break

                write_debug_log(debug_log, "TOOL_RESULT", {
                    "tool_name": tool_name,
                    "tool_id": tool_id,
                    "text_preview": serialized["text"][:300],
                    "images_count": len(serialized["images"]),
                    "source": source,
                    "registry_remaining": len(tool_call_registry)
                })

                # 发送 tool_end 事件，标记工具调用结束
                yield create_sse_message({
                    "type": "tool_end",
                    "tool_name": tool_name,
                    "tool_call_id": tool_id,
                    "source": source
                })

                # 确保展示消息中的工具标记为 done（兜底）
                for dm in reversed(display_messages):
                    if dm["role"] == "tool" and dm["id"] == tool_id and dm["tool_status"] == "calling":
                        dm["tool_status"] = "done"
                        break

            # 处理 AI 文本内容
            # 注意：ToolMessage（type == "tool"）的内容是工具执行结果，已在上面
            # tool_result 事件中处理。这里必须跳过，否则工具结果会被当作 AI 文本重复输出。
            content_text = extract_content_from_token(token)
            has_tool_calls = hasattr(token, 'tool_call_chunks') and token.tool_call_chunks
            is_tool_result = hasattr(token, 'type') and token.type == "tool"

            if content_text and not has_tool_calls and not is_tool_result:
                collected_content += content_text
                yield create_sse_message({
                    "type": "token",
                    "content": content_text,
                    "source": source
                })
                write_debug_log(debug_log, "TOKEN", {
                    "content": content_text[:200],
                    "source": source
                })

                # 更新展示消息：如果最后一条是 assistant 则追加，否则新建
                if _last_display_is_assistant():
                    display_messages[-1]["content"] += content_text
                    display_messages[-1]["source"] = source
                else:
                    display_messages.append({
                        "id": f"assistant-{uuid.uuid4()}",
                        "role": "assistant",
                        "content": content_text,
                        "source": source
                    })

        # ---- 流正常结束（无中断）----
        # 兜底：将所有 calling 状态的工具标记为 done
        for dm in display_messages:
            if dm["role"] == "tool" and dm["tool_status"] == "calling":
                dm["tool_status"] = "done"

        # 清理空的 assistant 消息
        display_messages = [
            dm for dm in display_messages
            if not (dm["role"] == "assistant" and not dm.get("content"))
        ]

        # 保存完整展示消息到 PostgreSQL（包含子代理消息）
        # 多轮对话：追加到已有消息，而非覆盖（每轮调用 save 时 display_messages 仅含当前轮）
        if resume_data is None:
            # 初始对话：追加到已有历史
            existing = await agent_loader.get_display_messages(thread_id, user_id=user_id) or []
            all_messages = existing + display_messages
        else:
            # resume 模式：display_messages 已包含历史（load 时获取），直接保存
            all_messages = display_messages
        await agent_loader.save_display_messages(thread_id, all_messages, user_id=user_id)
        write_debug_log(debug_log, "SAVE_DISPLAY", {
            "thread_id": thread_id,
            "total_count": len(all_messages)
        })

        # 流结束，发送 done 事件
        write_debug_log(debug_log, "STREAM_DONE", {
            "thread_id": thread_id,
            "total_content_len": len(collected_content)
        })

        yield create_sse_message({
            "type": "done",
            "thread_id": thread_id,
            "content": collected_content
        })

    except Exception as e:
        write_debug_log(debug_log, "STREAM_ERROR", {"error": str(e)})
        yield create_sse_message({
            "type": "error",
            "message": str(e)
        })


# ============================================================
# API 端点
# ============================================================

@router.post("/chat/stream")
async def chat_stream(
    request: ChatRequest,
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
    x_username: Optional[str] = Header(None, alias="X-Username"),
):
    """
    流式对话接口

    接收用户消息，返回 SSE 流式响应
    支持实时显示 AI 生成的内容和工具调用信息
    检测到 Human-in-the-Loop 中断时会发送 interrupt 事件

    身份从请求头 X-User-Id / X-Username 解析（已做白名单校验与兜底），
    决定会话归属与记忆命名空间。
    """
    identity = resolve_identity(x_user_id, x_username)
    thread_id = request.thread_id or str(uuid.uuid4())

    return StreamingResponse(
        stream_chat_response(
            message=request.message,
            thread_id=thread_id,
            user_id=identity.user_id,
            username=identity.username,
        ),
        media_type="text/event-stream",  # text/json, text/html, image/jpg
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@router.post("/chat/{thread_id}/resume")
async def chat_resume(
    thread_id: str,
    request: ResumeRequest,
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
    x_username: Optional[str] = Header(None, alias="X-Username"),
):
    """
    中断恢复接口

    当中断发生后，前端收集用户决策/补充数据，通过此端点恢复 Agent 执行。
    request.resume 的格式取决于中断类型：
    - 数据补充: {"supplement": "用户输入的补充信息"}
    - HITL 审批: {"decisions": [{"type": "approve"}]} 或 [{"type": "reject"}]

    防护：内部会校验会话归属（越权返回 error 事件），并确认确实存在待处理中断，
    避免"重复点审批"把写操作执行两遍。
    """
    identity = resolve_identity(x_user_id, x_username)
    return StreamingResponse(
        stream_chat_response(
            thread_id=thread_id,
            resume_data=request.resume,
            user_id=identity.user_id,
            username=identity.username,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@router.get("/chat/{thread_id}")
async def get_chat_state(
    thread_id: str,
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
):
    """获取会话状态（先校验会话归属）"""
    try:
        identity = resolve_identity(x_user_id, None)
        if not agent_loader.check_thread_access(thread_id, identity.user_id):
            raise HTTPException(status_code=403, detail="无权访问该会话")

        messages = await agent_loader.get_current_messages(thread_id)

        message_list = []
        for i, msg in enumerate(messages):
            # P0 修复：这里的 msg 是 LangGraph 返回的 BaseMessage 对象，不是 dict，
            # 原实现 msg.get("role") 会直接 AttributeError（BaseMessage 无 get），
            # 导致该 GET 接口必然 500。统一用 history.py 的兼容辅助函数读取属性，
            # 同时兼容 dict 形态的历史数据。
            message = Message(
                id=f"msg-{i}",
                role=get_message_role(msg),
                content=get_message_content(msg),
                created_at=datetime.now(),
                tool_calls=get_message_attr(msg, "tool_calls"),
                tool_call_id=get_message_attr(msg, "tool_call_id"),
            )
            message_list.append(message)

        return ChatResponse(
            thread_id=thread_id,
            messages=message_list
        )

    except HTTPException:
        # 上面自己抛的 403 必须原样透传，不能再被下面的兜底包成 500
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/chat/{thread_id}/history")
async def get_chat_history(
    thread_id: str,
    limit: int = Query(50, ge=1, le=100),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
):
    """获取会话历史状态列表（先校验会话归属）"""
    try:
        identity = resolve_identity(x_user_id, None)
        if not agent_loader.check_thread_access(thread_id, identity.user_id):
            raise HTTPException(status_code=403, detail="无权访问该会话")

        states = await agent_loader.get_state_history(thread_id, limit)

        return {
            "thread_id": thread_id,
            "states": [
                {
                    "config": state.config,
                    "values": state.values,
                    "created_at": state.created_at,
                    "parent_config": state.parent_config
                }
                for state in states
            ]
        }

    except HTTPException:
        # 上面自己抛的 403 必须原样透传，不能再被下面的兜底包成 500
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
