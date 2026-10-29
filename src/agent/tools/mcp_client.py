"""
MCP 工具客户端。

在 Agent 启动时连接所有 MCP Server，获取全部 MCP 工具，
并按分组筛选后分配给不同的子 Agent。

使用方式:
    from agent.tools.mcp_client import load_mcp_tools

    all_tools, analyst_tools, order_tools, chart_tools = await load_mcp_tools()
"""

import logging
import os
from typing import List, Tuple

from langchain_mcp_adapters.client import MultiServerMCPClient

logger = logging.getLogger(__name__)

# ---------------- MCP Server 连接配置 ----------------
# 注意：这里不再写死第三方托管端点。原实现把图表 MCP 固定指向某个
# 第三方"模型推理托管"地址（URL 里带个人应用 ID）——应用 ID 属于个人资源、
# 随时可能失效，也不该随代码分发。现在统一由环境变量提供：
#   ERP_MCP_URL / ERP_MCP_SERVER_NAME          业务 MCP（默认本地 8000）
#   CHART_MCP_URL / CHART_MCP_SERVER_NAME      图表/可视化 MCP（不配置则跳过）
# 未配置图表端点时只连 ERP，工具池少一批图表工具，但 Agent 仍能正常启动。
ERP_MCP_URL = os.environ.get("ERP_MCP_URL", "http://127.0.0.1:8000/mcp").strip()
ERP_MCP_SERVER_NAME = os.environ.get("ERP_MCP_SERVER_NAME", "erp-api").strip() or "erp-api"
CHART_MCP_URL = os.environ.get("CHART_MCP_URL", "").strip()
CHART_MCP_SERVER_NAME = os.environ.get("CHART_MCP_SERVER_NAME", "chart").strip() or "chart"


def _default_server_config() -> dict:
    """按环境变量构造默认配置（图表端点为空时不包含对应 server）。"""
    config = {
        ERP_MCP_SERVER_NAME: {
            "url": ERP_MCP_URL,
            "transport": "streamable_http",
        },
    }
    if CHART_MCP_URL:
        config[CHART_MCP_SERVER_NAME] = {
            "url": CHART_MCP_URL,
            "transport": "streamable_http",
        }
    return config


# 模块级默认配置（保留原名字，供外部引用；实际加载时重新读取环境变量）
MCP_SERVER_CONFIG = _default_server_config()

# 工具分组规则（前缀匹配）
# supplier_/material_/inventory_ 归分析类，order_ 归订单类：
# 这样"查"和"写"在物理上就分到两个子 Agent，写操作只走带审批的那条链路。
ANALYST_TOOL_PREFIXES = ("supplier_", "material_", "inventory_", "project_")
ORDER_TOOL_PREFIXES = ("order_",)
CHART_TOOL_PREFIXES = ("generate_",)  # 图表 MCP 的可视化工具


async def load_mcp_tools(
    server_config: dict | None = None,
) -> Tuple[List, List, List, List]:
    """
    连接到所有 MCP Server，加载全部工具并分组。

    Args:
        server_config: MCP Server 连接配置，默认按环境变量构造（见 _default_server_config）。

    Returns:
        (all_tools, analyst_tools, order_tools, chart_tools) 四元组
        - all_tools: 全部 MCP 工具列表（ERP + 图表）
        - analyst_tools: 供应商查询 + 物料查询/比价 + 库存预警工具
        - order_tools: 订单创建 + 订单更新 + 订单搜索工具
        - chart_tools: 图表/地图/可视化生成工具（来自图表 MCP Server，未配置时为空）
    """
    config = server_config if server_config is not None else _default_server_config()
    if not config:
        print("[WARNING] 没有任何 MCP Server 配置，跳过 MCP 工具加载")
        return [], [], [], []

    # server 名称解析：优先用配置名，其次退化为配置里的第一个；
    # 图表 server = 除 ERP 之外的第一项（不写死任何第三方名字）。
    erp_name = ERP_MCP_SERVER_NAME if ERP_MCP_SERVER_NAME in config else next(iter(config))
    chart_name = next((name for name in config if name != erp_name), None)

    print("[INFO] 正在连接 MCP Server...")
    mcp_client = MultiServerMCPClient(config)

    # 从 ERP MCP Server 获取业务工具（连不上属于致命错误，交由上层处理）
    erp_tools = await mcp_client.get_tools(server_name=erp_name)
    print(f"[INFO] 已从 ERP MCP Server({erp_name}) 加载 {len(erp_tools)} 个工具")

    # 从图表 MCP Server 获取可视化工具（可选，失败只降级）
    analysis_tools: List = []
    if chart_name:
        try:
            analysis_tools = await mcp_client.get_tools(server_name=chart_name)
            print(f"[INFO] 已从图表 MCP Server({chart_name}) 加载 {len(analysis_tools)} 个工具")
        except Exception:
            logger.exception("图表 MCP Server(%s) 加载失败，已跳过图表工具", chart_name)
    else:
        print("[INFO] 未配置 CHART_MCP_URL，跳过图表 MCP Server")

    # 合并全部工具
    all_tools = list(erp_tools) + list(analysis_tools)

    # 按前缀分组：业务工具
    analyst_tools = [
        t for t in erp_tools
        if t.name.startswith(ANALYST_TOOL_PREFIXES)
    ]
    order_tools = [
        t for t in erp_tools
        if t.name.startswith(ORDER_TOOL_PREFIXES)
    ]

    # 图表工具（来自外部可视化 MCP Server，未配置时为空）
    chart_tools = [
        t for t in analysis_tools
        if t.name.startswith(CHART_TOOL_PREFIXES)
    ]

    print(
        f"[INFO] 工具分组完成: "
        f"分析类 {len(analyst_tools)} 个, "
        f"订单类 {len(order_tools)} 个, "
        f"图表类 {len(chart_tools)} 个"
    )

    return all_tools, analyst_tools, order_tools, chart_tools
