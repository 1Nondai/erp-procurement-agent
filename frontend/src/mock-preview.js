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

const answer = `## 台面比价结论

两种台面报价已拉到，**环科新材的理化板综合最优**：单价最低且交期稳定。

| 供应商 | 台面类型 | 单价（元/㎡） | 交期 | 可供应量 |
| --- | --- | --- | --- | --- |
| 环科新材 | 12.7mm 实芯理化板 | 236.00 | 7 天 | 1200 |
| 华瑞板业 | 12.7mm 实芯理化板 | 248.00 | 5 天 | 860 |
| 科洁台面 | 13mm 环氧树脂板 | 412.00 | 12 天 | 430 |

关键判断：

1. 环氧树脂板单价高出理化板约 74%，仅在强耐蚀工位选型时值得用
2. 科洁台面交期 12 天，覆盖不了本项目 8 天的进场节点
3. 环科新材单价最低，且 \`avgScore = 8.4\`（QSDC 四维平均）

> 建议：常规实验台台面按环科新材的理化板下单 260 ㎡，环氧树脂板仅备 30 ㎡ 用于耐蚀台。

\`\`\`python
# 成本测算（含运费）
def total_cost(unit_price, qty, freight=0):
    return unit_price * qty + freight
\`\`\`

已为你生成对比图，可点开查看。`

const userMsg = {
  role: 'user',
  content: '帮我对比一下理化板台面和环氧树脂板台面的供应商报价，交期也要看。'
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
  args: { url: 'https://example.com/worktop-price', selector: '.price-table' }
}

const toolDone = {
  role: 'tool',
  tool_name: 'create_procurement_chart',
  source: 'procurement_analyst',
  tool_status: 'done',
  args: { chart_type: 'bar', title: '理化板 / 环氧树脂板台面单价对比', data: [[236.0, 248.0, 412.0]] },
  text: '图表已生成：worktop_price_comparison.png'
}

const supplementInterrupt = {
  interrupt_type: 'order_info_supplement',
  missing_fields: '物料ID、数量、单价',
  collected_data: { supplier: '环科新材', orderDate: '2026-06-15' }
}

const approvalInterrupt = {
  interrupt_type: 'hitl_approval',
  action_requests: [
    {
      name: 'order_create',
      args: {
        supplierName: '环科新材',
        orderDate: '2026-06-15',
        totalAmount: 64160.0,
        orderDetail: [
          { partId: 100, quantity: 260, unitPrice: 236.0, subtotal: 61360 },
          { partId: 217, quantity: 30, unitPrice: 93.33, subtotal: 2800 }
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
