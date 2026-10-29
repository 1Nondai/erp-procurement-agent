"""RAG 子包：知识库构建、向量检索与 Agent 工具封装。

为什么用惰性导出（PEP 562）而不是在包入口直接 import：
    `rag.knowledge_base` 会拉起 numpy 与智谱 SDK——这两个都是重依赖。
    只要有人 `from rag.knowledge_docs import ...`（纯文本语料，本身零依赖），
    包入口就会被迫加载 numpy，导致"只想读语料"或"裸环境跑一致性测试"直接 ImportError。
    CI 就是用裸环境跑 tests/ 的，这个坑在本地有 numpy 时完全看不出来。

用法不变：from rag import KnowledgeBase
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅类型检查/IDE 补全时导入，运行期不触发
    from rag.knowledge_base import KnowledgeBase, ZhipuEmbedding

__all__ = ["KnowledgeBase", "ZhipuEmbedding"]


def __getattr__(name: str):
    """按需导出：第一次真正用到时才加载 knowledge_base（连带 numpy）。"""
    if name in __all__:
        from rag import knowledge_base

        return getattr(knowledge_base, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
