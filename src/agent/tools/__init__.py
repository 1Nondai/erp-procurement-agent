"""
Agent 工具集注册表。

集中导出所有本地（非 MCP）工具，main_agent 只从这里 import，
避免工具散落在各处被重复引入。
"""

from agent.tools.web_search import web_search
from agent.tools.chart_generator import create_generate_chart_tool, CHART_TYPE_GUIDE
from agent.tools.knowledge_search import knowledge_search

__all__ = [
    "web_search",
    "create_generate_chart_tool",   # 工厂函数，返回 (generate_visualization, 其他工具)
    "CHART_TYPE_GUIDE",
    "knowledge_search",
]
