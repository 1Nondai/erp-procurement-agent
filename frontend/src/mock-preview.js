/**
 * 组件视觉预览（仅开发期用）
 * ------------------------------------------------------------
 * 目的：在没有后端的情况下，把消息气泡 / 工具卡片 / 中断横幅
 * 用真实 fixture 渲染出来，肉眼核对间距、配色与层级。
 * 不参与生产构建（入口是 index.html），验证完即可删除。
 */
import { createApp, h } from 'vue'
import './styles/main.css'
import MessageItem from './components/MessageItem.vue'
import InterruptBanner from './components/InterruptBanner.vue'

const answer = `## 火花塞比价结论

三家报价已拉到，**NGK 综合最优**：单价最低且交期稳定。

| 品牌 | 单价（元） | 交期 | 库存 |
| --- | --- | --- | --- |
| NGK | 25.50 | 5 天 | 1200 |
| 博世 | 28.00 | 7 天 | 860 |
| 电装 | 26.80 | 12 天 | 430 |

关键判断：

1. 电装交期 12 天，无法覆盖当前 8 天安全库存缺口
2. 博世单价高 9.8%，但供应商评分最高
3. NGK 单价最低，且 \`supplier_rating = 4.6\`

> 建议：本次按 NGK 下单 500 支，博世保留为备选供应商。

\`\`\`python
# 成本测算（含运费）
def total_cost(unit_price, qty, freight=0):
    return unit_price * qty + freight
\`\`\`

已为你生成对比图，可点开查看。`

const userMsg = {
  role: 'user',
  content: '帮我对比一下 NGK、博世、电装三家火花塞的报价，交期也要看。'
}

const mainMsg = { role: 'assistant', source: 'main', content: answer }
const subMsg = {
  role: 'assistant',
  source: 'procurement_analyst',
  content: '子 Agent 已完成三家供应商报价抓取，共解析 3 个页面、17 条有效报价记录。'
}

const toolCalling = {
  role: 'tool',
  tool_name: 'web_scraper',
  source: 'main',
  tool_status: 'calling',
  args: { url: 'https://example.com/ngk-price', selector: '.price-table' }
}

const toolDone = {
  role: 'tool',
  tool_name: 'create_procurement_chart',
  source: 'procurement_analyst',
  tool_status: 'done',
  args: { chart_type: 'bar', title: '三家火花塞单价对比', data: [[25.5, 28.0, 26.8]] },
  text: '图表已生成：spark_plug_price_comparison.png'
}

const supplementInterrupt = {
  interrupt_type: 'order_info_supplement',
  missing_fields: '物料ID、数量、单价',
  collected_data: { supplier: 'NGK 中国', orderDate: '2026-06-15' }
}

const approvalInterrupt = {
  interrupt_type: 'hitl_approval',
  action_requests: [
    {
      name: 'order_create',
      args: {
        supplierName: 'NGK 中国',
        orderDate: '2026-06-15',
        totalAmount: 12750.0,
        orderDetail: [
          { partId: 100, quantity: 500, unitPrice: 25.5, subtotal: 12750 },
          { partId: 217, quantity: 40, unitPrice: 88.0, subtotal: 3520 }
        ]
      }
    }
  ]
}

const Label = (text) => h('div', { class: 'preview-label' }, text)

const section = (title, ...children) => [Label(title), ...children]

const App = {
  setup() {
    return () =>
      h('div', { class: 'preview-wrap' }, [
        ...section(
          '用户消息 / 主 Agent / 子 Agent',
          h(MessageItem, { message: userMsg }),
          h(MessageItem, { message: mainMsg }),
          h(MessageItem, { message: subMsg })
        ),
        ...section(
          '工具调用：执行中 / 已完成',
          h(MessageItem, { message: toolCalling }),
          h(MessageItem, { message: toolDone })
        ),
        ...section(
          '中断：数据补充',
          h(InterruptBanner, { interruptData: supplementInterrupt })
        ),
        ...section(
          '中断：HITL 审批',
          h(InterruptBanner, { interruptData: approvalInterrupt })
        )
      ])
  }
}

createApp(App).mount('#app')
