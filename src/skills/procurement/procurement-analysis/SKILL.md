---
name: procurement-analysis
description: >
  采购分析主技能包。串联完整分析流程：需求拆解 → 数据收集 → 执行分析 → 生成图表 → 输出报告。
  用于实验设备材料（台面/板材/钢材/五金/玻璃/水暖/电气/通风/涂装/辅材）的供应商对比、
  价格分析、物料筛选、库存补货与采购策略评估。
---

# 采购分析技能（操作手册）

## 适用场景
- 供应商比价分析（同一物料多家报价对比，如理化板台面在威盛亚/创星/南陶之间的报价）
- 物料行情评估（冷轧钢板、钢化玻璃等材料的价格走势与供需）
- 供应商综合评分（按信用评级、QSDC 四维评分、交期综合打分）
- 库存补货建议（依据 inventory_warning 的缺口与提前期排序）
- 采购策略建议（分阶段采购、集中采购、备选供应商推荐）
- 生成含图表的采购分析报告

## 分析流程（5 步）

### 第 1 步：理解需求
- 从任务描述中提取：【任务目标】、【用户偏好】、【分析需求正文】
- 明确分析维度：供应商对比？价格趋势？物料筛选？库存补货？综合评估？
- 明确物料范围：台面 / 板材 / 钢材 / 五金 / 玻璃 / 水暖 / 电气 / 通风 / 涂装 / 辅材

### 第 2 步：收集数据

**2a. ERP 内部数据（必须，来自 MCP 工具，数据源是 PostgreSQL erp schema）**

| 目的 | 工具 | 用法示例 |
|------|------|----------|
| 查物料与当前报价 | `material_query` | `material_query(name="理化板")` 或 `material_query(category="台面", size=50)` |
| 多家报价对比 | `material_price_compare` | `material_price_compare(material_name="冷轧钢板")` 或 `material_price_compare(material_id=6)` |
| 查供应商资质与评分 | `supplier_query` | `supplier_query(category="五金", min_credit="B")` |
| 库存缺口 | `inventory_warning` | `inventory_warning(level="CRITICAL")` |
| 历史成交价 | `order_search_details` | `order_search_details(material_name="钢化玻璃", start_date="2026-01-01")` |

要点：
- 物料分类只能取这十个值：台面 / 板材 / 钢材 / 五金 / 玻璃 / 水暖 / 电气 / 通风 / 涂装 / 辅材
- `material_query` 的 `name` 支持名称/规格/品牌模糊匹配（"理化板"、"1.0mm"、"冷轧"、"威盛亚"）
- 物料 ID 必须来自 `material_query` 的 `records[].id`，后续所有环节都用这个 ID
- 比价的结论性数字**必须取自 `material_price_compare` 的 `summary`**：
  `supplierCount`（几家报价）、`lowestSupplier`/`lowestPrice`、`highestSupplier`/`highestPrice`、
  `spreadPct`（价差百分比）、`expiredQuotes`（过期报价数）、`recommendation`
  —— **不得自己从 quotes 数组心算最低价、均价或价差**，口径只在工具里算一次
- 过期报价（`isExpired: true`）不参与最低价判定；引用时必须注明"已过期"

**2b. 外部数据（不可跳过）**
1. 激活 `/skills/procurement/supplier-price-urls/`，读取 `data/url_mapping.yaml`
2. 按物料名（如"理化板台面 1220×2440×13mm 威盛亚"）查找对应询价站点 URL
3. **找到 URL** → 使用 `web-scraper` 直接抓取：
   ```bash
   execute("python /skills/procurement/web-scraper/scrape_page.py --url '<询价URL>'")
   ```
   脚本将 HTML 转为 Markdown 保存到 `/analysis/temp/{filename}.md`，再 `read_file` 读取内容提取价格/规格/品牌/材质/单位。
4. **未找到 URL** → 使用 `web_search` 搜索该物料的市场价格与规格参数
5. 无论哪种路径，最终都要有外部参考数据用于对比；外部市场价与 ERP 报价**分开列示**，不得混为一谈

### 第 3 步：执行分析
1. 编写 Python 分析脚本，使用 `execute` 工具运行
2. 如需安装依赖：`pip install -i https://mirrors.aliyun.com/pypi/simple/ pandas matplotlib numpy`（已自动路由到 `/opt/skills-venv/bin/python`）
3. 常见分析维度：
   - 价格对比：引用 `summary` 的最低价/最高价/价差，不要重算
   - 供应商评估：信用评级（A/B/C/D）+ QSDC 四维评分（quality/service/delivery/cost）+ 平均交期
   - 物料质量：规格参数对比（如台面 13mm vs 19mm、冷轧板 1.0mm vs 1.2mm）
   - 综合排名：多维度加权评分（注明权重来源是用户偏好还是默认）
   - 库存维度：缺口 `shortage` / 安全库存比 `stockRatio` / 提前期 `leadTimeDays`
4. 分析结果保留在上下文（`execute` 工具输出），无需写入文件

### 第 4 步：生成图表
1. **首次调用图表前**：`read_file("/skills/procurement/chart_params.md")` 获取 26 种图表参数速查（紧凑格式，仅 data 格式 + 特有参数，通用参数已提取到顶部表格，不逐图重复）
2. 根据分析维度选择 2-4 个最有洞察力的 chart_type
3. 循环调用 `generate_visualization(chart_type="xxx", chart_config={...})`，通用参数（width/height/title/theme/style）直接使用，无需每图查阅
4. 参考文件只读一次，后续多次调用不额外消耗上下文
5. 参数报错时对照参考文件修正后重试

### 第 5 步：生成报告
1. 汇总分析结论和图表 URL
2. 写入 `/analysis/report_{timestamp}.md`

## 图表类型速查

| 分析目的 | chart_type | 说明 |
|----------|------------|------|
| 供应商价格横向对比 | bar | 各供应商对同一物料报价（如理化板台面的 3 家报价） |
| 物料采购量/金额对比 | column | 按物料或分类（台面/钢材/五金）的柱状对比 |
| 价格/订单量趋势 | line | 冷轧板、玻璃等材料的时间序列走势 |
| 采购金额占比 | pie | 按分类或供应商的金额占比 |
| 供应商多维度评分 | radar | QSDC 四维（质量/服务/交付/成本） |
| 价格分布 | histogram | 同类物料报价区间分布 |
| 采购金额层级 | treemap | 分类 → 物料层级占比 |
| 供应商-物料关系 | network_graph | 供应关系拓扑（谁供哪类材料） |
| 交货周期分布 | boxplot | 各供应商 avgDeliveryDays 分布 |
| 成本构成增减 | waterfall | 采购成本逐项瀑布图 |
| 预算完成率 | liquid | 水波图，单一百分比指标 |
| 采购流程转化 | funnel | 各环节转化漏斗 |

> 完整 26 种图表及参数 schema 见 `/skills/procurement/chart_params.md`

## 报告模板

```markdown
# 采购分析报告

## 1. 分析概述
（目的、数据范围、方法说明）

## 2. 数据概览
（查询到的供应商数量、物料数量、数据时间范围）

## 3. 对比分析
（核心对比结果，附 material_price_compare 的 summary 数字与图表引用）

## 4. 结论与建议
（3-5 条可操作的采购建议，含推荐供应商与预计交期）
```

## 返回主 Agent 格式

```python
【报告路径】/analysis/report_xxx.md
【摘要】（300-500 字核心发现）
【结论】1. … 2. … 3. …
【建议】- 具体可执行的采购建议
```

**不要**返回原始工具输出或 JSON 数据。

## 示例：冷轧钢板比价（口径演示）

```
1. material_query(name="冷轧", category="钢材")     → 命中 MAT-0006 冷轧钢板 SPCC 1.0mm，id=6
2. material_price_compare(material_id=6)          → summary: supplierCount=2,
   lowestSupplier=宝钢, lowestPrice=<工具返回>, spreadPct=<工具返回>, expiredQuotes=0
3. supplier_query(category="钢材")                 → 宝钢 A 级、avgDeliveryDays=7、月结30天
4. 结论写法："共 2 家有效报价，宝钢最低 <引用 lowestPrice>，较最高价低 <引用 spreadPct>%，
   交期 7 天，信用 A 级，建议走宝钢"
```

> 注意：结论里的数字全部引用工具返回，不出现自己算出来的数；物料 ID 用 6 而不用"冷轧钢板"这类名称。

## 文件管理说明

- DeepAgents 已内置自动 offload（>20k tokens）和 Summarization（85% 上下文阈值）
- 你只需显式写入：`/analysis/report_*.md`
- 其余工具返回由框架自动管理

## 分析资源

### 脚本模板
- `scripts/price_compare.py` — 价格对比分析
- `scripts/supplier_scoring.py` — 供应商综合评分

## 关键原则
- **技能发现**：每次任务开始先 `ls /skills/procurement/` 扫描可用技能，不依赖静态列表
- **比价引用 summary**：最低价/价差/供应商家数一律引用 `material_price_compare.summary`，不心算
- **先查后用 ID**：报告里出现的物料编号必须来自 `material_query`，不得编造
- **数据真实**：所有结论基于真实数据，不编造价格与供应商
- **精简输出**：报告不超过 500 字摘要 + 3-5 条结论
- **成本意识**：先获取必要数据，避免无限循环查询

## 环境依赖
- Python 3.11+
- pandas, matplotlib, numpy（如未安装：`pip install -i https://mirrors.aliyun.com/pypi/simple/ pandas matplotlib numpy`）
