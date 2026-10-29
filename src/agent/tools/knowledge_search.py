"""
知识库检索工具（RAG 工具封装）。

把 rag.KnowledgeBase 包装成 LangChain Tool（knowledge_search），
供主 Agent / 子 Agent 在回答领域问题前先检索知识库。

为什么放在工具层而不是直接把知识注入 system prompt：
    1. 知识库会持续扩充，全量注入 prompt 会撑爆上下文窗口；
       按需检索（top-k）只把相关片段带进本轮对话；
    2. 检索结果带 source 元数据，Agent 可以在回答中引用出处，
       降低幻觉风险；
    3. 与 MCP 工具同构——将来知识库迁移到独立服务时只改这一层。
"""

from __future__ import annotations

import logging

from langchain_core.tools import tool

from rag.knowledge_base import KnowledgeBase

logger = logging.getLogger(__name__)

# 进程级单例：知识库（含向量索引）构建有成本（嵌入网络调用），
# 全局只建一次；后续请求共享内存索引。
_kb_instance: KnowledgeBase | None = None


def get_knowledge_base() -> KnowledgeBase:
    """获取全局 KnowledgeBase 单例（惰性创建）。

    单例的原因：KnowledgeBase 内部持有全量向量索引（numpy 矩阵），
    多实例会重复占内存并各自维护过期副本。
    """
    global _kb_instance
    if _kb_instance is None:
        _kb_instance = KnowledgeBase()
    return _kb_instance


@tool("knowledge_search", parse_docstring=True)
async def knowledge_search(query: str, top_k: int = 5) -> str:
    """在 ERP 采购知识库中检索与问题相关的领域知识。

    适用于：采购制度与流程疑问（订单状态、审批要求、比价原则）、
    摩托车零部件专业知识（火花塞/刹车片/机油/轮胎/蓄电池选型）、
    供应商评估方法、库存管理策略（安全库存、ABC 分类、呆滞处理）。

    回答领域问题前应先调用本工具获取依据，而不是凭记忆作答；
    检索结果会标注来源（source 字段），回答时请注明出处。

    Args:
        query: 要检索的问题或关键词（自然语言）。
        top_k: 返回最相关片段的数量，默认 5。

    Returns:
        格式化的知识片段列表（含来源与相关度分数）；
        无相关知识或检索失败时返回说明文字。
    """
    try:
        kb = get_knowledge_base()
        # 空库时先构建默认语料（幂等，只发生一次）
        await kb.ensure_built()
        results = await kb.search(query, top_k=max(1, min(top_k, 10)))

        if not results:
            return "知识库中暂无与该问题相关的内容。"

        # 输出格式刻意稳定：模型对固定格式更稳定，也方便评测脚本断言
        lines = []
        for i, r in enumerate(results, 1):
            score = f"{r['score']:.3f}"
            lines.append(f"[{i}] (相关度 {score}，来源: {r['meta'].get('source', '未标注')})\n{r['text']}")
        return "\n\n".join(lines)

    except Exception as e:  # noqa: BLE001
        # 工具层吞异常返回说明文字：检索失败不应让整个 Agent 运行崩溃，
        # Agent 看到"检索失败"可以选择继续用通用知识回答
        logger.warning("knowledge_search 检索失败: %s", e, exc_info=True)
        return f"知识库检索失败: {e}"
