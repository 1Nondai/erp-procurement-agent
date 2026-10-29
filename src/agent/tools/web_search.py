"""
自定义网络搜索工具。

基于智谱 AI 的搜狗 Web Search API，供主 Agent 和所有子 Agent 使用。
"""

import asyncio
import logging

from langchain_core.tools import tool
from zai import ZhipuAiClient

from agent.env_utils import ZHIPU_API_KEY

logger = logging.getLogger(__name__)

_client = None


def _get_client():
    """懒加载 ZhipuAiClient，避免未配置 API Key 时导入失败。"""
    global _client
    if _client is None:
        if not ZHIPU_API_KEY:
            raise RuntimeError("ZHIPU_API_KEY 未配置，web_search 工具不可用")
        _client = ZhipuAiClient(api_key=ZHIPU_API_KEY)
    return _client


@tool('web_search', parse_docstring=True)
async def web_search(query: str) -> str:
    """
    使用搜狗的API进行Web搜索。

    适用于：市场行情调研、供应商背景调查、物料价格趋势查询、行业资讯获取等。

    Args:
        query: 需要搜索的内容或者关键字。

    Returns:
        返回搜索之后的结果。
    """
    # 为什么必须是 async def + asyncio.to_thread：
    # zai SDK 是同步阻塞的 HTTP 调用（秒级），直接在 async 工具里跑会把整个
    # 事件循环卡住——SSE 流式响应期间所有并发请求一起停摆。
    # 丢进默认线程池后，事件循环可以继续处理其他会话。
    try:
        return await asyncio.to_thread(_search_sync, query)
    except Exception as e:
        # 用 logger.exception 而不是 print：保留堆栈便于排查，也不污染 stdout
        logger.exception("web_search 执行失败，query=%r", query)
        return f"搜索失败: {e}"


def _search_sync(query: str) -> str:
    """同步执行 SDK 搜索（阻塞式网络调用，只应在线程池中调用）。"""
    response = _get_client().web_search.web_search(
        search_engine="search_pro",
        search_query=query,
        count=3,  # 返回结果的条数，范围1-50，默认10
        search_recency_filter="noLimit",  # 搜索指定日期范围内的内容
    )
    if response.search_result:
        return "\n\n".join([d.content for d in response.search_result])
    return '没有搜索到任何内容！'
