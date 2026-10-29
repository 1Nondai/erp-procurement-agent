"""
子 Agent YAML 配置加载器。

从 YAML 配置文件目录读取子 Agent 定义，转换为 DeepAgents
create_deep_agent() 所需的 SubAgent dict 格式。

使用方式:
    from agent.subagents.loader import load_subagent_configs, resolve_subagent_tools

    # 1. 加载原始配置（tools 为字符串名称列表）
    raw_configs = load_subagent_configs()

    # 2. 与 MCP 工具匹配，解析为可调用的工具对象
    subagents = resolve_subagent_tools(raw_configs, all_mcp_tools)

P0 改进：工具名匹配从"无条件子串匹配 + 未命中只打 WARNING"改为
"显式匹配语法（精确 / prefix: / suffix: / glob:）+ 未命中即启动失败"。
动机与取舍见 resolve_subagent_tools 上方的说明块。
"""
from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Dict, List, Optional

import yaml

# YAML 配置文件目录
CONFIGS_DIR = Path(__file__).parent / "configs"


class SubagentToolResolutionError(RuntimeError):
    """子 Agent 声明的工具名无法解析为真实工具时抛出（fail fast）。

    单独定义异常类型（而不是裸 RuntimeError），是为了让上层能区分
    "配置写错"与"运行期故障"：配置错误应当在启动阶段直接终止进程，
    而不是等 Agent 跑起来后表现为"某个子 Agent 声称自己没有这个工具"。
    """


def load_subagent_configs(
    configs_dir: Optional[Path] = None,
) -> List[Dict]:
    """
    加载指定目录下所有 .yaml 子 Agent 配置文件。

    每个 YAML 文件的顶级结构:
        name: str               # 必须 — 子 Agent 唯一名称
        description: str        # 必须 — 供主 Agent 决策的描述
        system_prompt: str      # 必须 — 子 Agent 系统提示词
        tools: list[str]        # 必须 — 工具名称列表（运行时解析）
        model: str              # 可选 — 模型标识符
        skills: list[str]       # 可选 — 技能路径列表

    Args:
        configs_dir: 配置文件目录，默认为 CONFIGS_DIR。

    Returns:
        SubAgent 配置字典列表，tools 字段暂为字符串名称。
    """
    if configs_dir is None:
        configs_dir = CONFIGS_DIR

    configs: List[Dict] = []

    if not configs_dir.exists():
        print(f"[WARNING] 子 Agent 配置目录不存在: {configs_dir}")
        return configs

    for yaml_file in sorted(configs_dir.glob("*.yaml")):
        try:
            with open(yaml_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            print(f"[ERROR] 解析 YAML 文件失败: {yaml_file} — {e}")
            continue
        except Exception as e:
            print(f"[ERROR] 读取文件失败: {yaml_file} — {e}")
            continue

        # 校验必填字段
        missing = _validate_subagent_config(data, yaml_file.name)
        if missing:
            print(
                f"[ERROR] {yaml_file.name} 缺少必填字段: {', '.join(missing)}"
            )
            continue

        configs.append(data)
        print(f"[INFO] 已加载子 Agent 配置: {data['name']} ← {yaml_file.name}")

    return configs


# ============================================================
# 工具名匹配
# ============================================================
#
# 支持的四种声明语法（同一个 tools 列表里可混用）：
#   supplier_query      —— 精确名（推荐默认写法，语义最明确）
#   prefix:order_       —— 前缀匹配，适合"一次收编一组同类工具"
#   suffix:_query       —— 后缀匹配
#   glob:*_chart        —— 通配匹配（fnmatch，支持 * 与 ?）
#   注：不带前缀但含 * 或 ? 的写法也会按通配处理，方便手写。
#
# 为什么废弃原来的 `if pattern in tool_name`（无条件子串匹配）：
#   1. 过宽：写 `material` 会同时命中 material_query / material_price_comparey_supplier，
#      有时是想要的，有时是误伤（例如只想给 material_query）；
#   2. 静默扩容：上游新增 order_query_detail 之后，原本只想匹配 order_query 的
#      配置会突然多带一个工具，模型行为随之改变，而配置和代码都没动过；
#   3. 不可审计：无法区分"我有意收编一组工具"和"我手滑写宽了"。
# 显式语法把意图写进配置：精确是默认，宽匹配必须显式声明。


def _match_one_spec(spec: str, tool_index: Dict[str, object]) -> List[str]:
    """把一条工具声明解析成命中的工具名列表（保持 tool_index 顺序）。"""
    names = list(tool_index.keys())

    if spec.startswith("prefix:"):
        head = spec[len("prefix:"):]
        return [n for n in names if n.startswith(head)]

    if spec.startswith("suffix:"):
        tail = spec[len("suffix:"):]
        return [n for n in names if n.endswith(tail)]

    if spec.startswith("glob:"):
        pattern = spec[len("glob:"):]
        return [n for n in names if fnmatch.fnmatch(n, pattern)]

    # 无前缀：默认精确匹配；名字里含通配符时按通配处理
    if "*" in spec or "?" in spec:
        return [n for n in names if fnmatch.fnmatch(n, spec)]
    return [spec] if spec in tool_index else []


def resolve_subagent_tools(
    configs: List[Dict],
    available_tools: List,
    extra_middleware: Optional[Dict[str, List]] = None,
    strict: Optional[bool] = None,
) -> List[Dict]:
    """
    将 YAML 配置中的工具声明解析为实际的可调用工具对象。

    Args:
        configs: load_subagent_configs() 返回的原始配置列表。
        available_tools: 可用的工具对象列表（MCP 工具 + 本地工具）。
        extra_middleware: 子 Agent 额外中间件，key 为子 Agent 名称，value 为中间件实例列表。
        strict: 未命中是否抛错。None 时读取 config.SUBAGENT_TOOL_STRICT（默认 True）。
                暴露成参数有两个好处：便于单测，也避免本模块在导入时就拉起
                langchain / opensandbox 这些重依赖。

    Returns:
        可以直接传入 create_deep_agent(subagents=...) 的 SubAgent 字典列表。

    Raises:
        SubagentToolResolutionError: strict 模式下存在未命中的工具声明。
    """
    if extra_middleware is None:
        extra_middleware = {}

    if strict is None:
        # 惰性导入：让纯匹配逻辑可以脱离 langchain/opensandbox 单独测试
        try:
            from agent.config import SUBAGENT_TOOL_STRICT
            strict = SUBAGENT_TOOL_STRICT
        except Exception:
            strict = True

    # 构建工具名 → 工具对象的索引
    tool_index: Dict[str, object] = {}
    for t in available_tools:
        name = getattr(t, "name", None)
        if name:
            tool_index[name] = t

    subagents: List[Dict] = []
    unresolved: List[str] = []            # 汇总所有未命中，一次报全（避免改一个报一个）

    for config in configs:
        tool_specs = config.get("tools", []) or []
        matched_names: List[str] = []
        misses: List[str] = []

        for spec in tool_specs:
            hit_names = _match_one_spec(str(spec), tool_index)
            if not hit_names:
                misses.append(str(spec))
                continue
            for n in hit_names:
                if n not in matched_names:   # 去重且保持声明顺序
                    matched_names.append(n)

        if misses:
            unresolved.extend(f"{config.get('name')} ← {m}" for m in misses)

        resolved_tools = [tool_index[n] for n in matched_names]

        subagent: Dict = {
            "name": config["name"],
            "description": config["description"].replace("\n", " ").strip(),
            "system_prompt": config["system_prompt"],
            "tools": resolved_tools,
        }

        # 可选字段
        if config.get("model"):
            subagent["model"] = config["model"]

        if config.get("skills"):
            subagent["skills"] = config["skills"]

        if config.get("interrupt_on"):
            # HITL 审批：写操作执行前暂停等待 approve/reject，直接透传给 DeepAgents
            subagent["interrupt_on"] = config["interrupt_on"]

        # 合并额外中间件（代码侧注入：摘要中间件、调用上限等）
        agent_middleware = list(config.get("middleware", []))
        if config["name"] in extra_middleware:
            agent_middleware.extend(extra_middleware[config["name"]])
        if agent_middleware:
            subagent["middleware"] = agent_middleware

        subagents.append(subagent)

        # 把"最终给到这个子 Agent 的工具清单"打出来：启动日志即权限审计依据
        print(
            f"[INFO] 子 Agent '{config['name']}' 已解析: "
            f"{len(resolved_tools)} 个工具 "
            f"({', '.join(matched_names) if matched_names else '无'}), "
            f"{len(config.get('skills', []))} 个技能"
        )

    if unresolved:
        detail = "\n  - ".join(unresolved)
        available = ", ".join(sorted(tool_index.keys()))
        if strict:
            # 报错信息本身就是修复指引：把当前可用清单一起打出来
            raise SubagentToolResolutionError(
                "以下子 Agent 工具声明未匹配到任何真实工具（严格模式下启动终止）:\n"
                f"  - {detail}\n"
                f"当前可用工具（{len(tool_index)} 个）: {available}\n"
                "提示：精确名可直接写；批量收编请用 prefix:/suffix:/glob: 语法；"
                "若某个 MCP Server 尚未启动，可临时设置环境变量 SUBAGENT_TOOL_STRICT=0。"
            )
        print(
            "[WARNING] 以下工具声明未匹配（SUBAGENT_TOOL_STRICT=0，已降级为告警）:\n"
            f"  - {detail}"
        )

    return subagents


def _validate_subagent_config(data: Dict, filename: str) -> List[str]:
    """
    校验 SubAgent 配置的必填字段。

    Returns:
        缺失的字段名列表（空列表表示通过校验）。
    """
    required = ["name", "description", "system_prompt", "tools"]
    missing = [f for f in required if f not in data or data[f] is None]

    # tools 必须是非空列表
    if "tools" in data:
        tools = data["tools"]
        if not isinstance(tools, list) or len(tools) == 0:
            missing.append("tools (必须为非空列表)")

    # system_prompt 不能为空字符串
    if "system_prompt" in data:
        sp = data["system_prompt"]
        if not isinstance(sp, str) or not sp.strip():
            missing.append("system_prompt (必须为非空字符串)")

    return missing
