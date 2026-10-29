<template>
  <div class="interrupt-banner" :class="bannerClass">
    <!-- ============================================================ -->
    <!-- 类型 1：数据补充中断（request_order_info） -->
    <!-- ============================================================ -->
    <template v-if="interruptData.interrupt_type === 'order_info_supplement'">
      <div class="interrupt-header">
        <svg class="interrupt-icon" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M8 4.6h8M9.4 4.6v2.2M14.6 4.6v2.2M6 6.8h12a1.4 1.4 0 0 1 1.4 1.4v10.4a1.4 1.4 0 0 1-1.4 1.4H6a1.4 1.4 0 0 1-1.4-1.4V8.2A1.4 1.4 0 0 1 6 6.8zM8.4 11h7.2M8.4 14.6h4.6" />
        </svg>
        <span class="interrupt-title">订单数据不完整，请补充以下信息</span>
      </div>

      <div class="interrupt-body">
        <div class="info-block missing-fields">
          <div class="info-label">缺少字段</div>
          <div class="info-value">{{ interruptData.missing_fields }}</div>
        </div>
        <div class="info-block collected-data" v-if="interruptData.collected_data">
          <div class="info-label">已收集数据</div>
          <pre class="info-pre">{{ formatCollectedData(interruptData.collected_data) }}</pre>
        </div>
      </div>

      <div class="interrupt-input-area">
        <textarea
          ref="supplementInputRef"
          v-model="supplementText"
          placeholder="请用自然语言描述要补充的信息，例如：物料ID=100、数量=50、单价25.5元、预计6月15日交货..."
          class="supplement-textarea"
          rows="3"
          @keydown.enter.exact="handleSupplementEnter"
        ></textarea>
        <button
          class="submit-btn supplement-btn"
          @click="handleSupplement"
          :disabled="!supplementText.trim() || submitting"
        >
          {{ submitting ? '提交中...' : '提交补充信息' }}
        </button>
      </div>
      <div class="interrupt-hint">按 Enter 提交，Shift + Enter 换行</div>
    </template>

    <!-- ============================================================ -->
    <!-- 类型 2：HITL 审批中断（order_create / order_update） -->
    <!-- ============================================================ -->
    <template v-else-if="interruptData.interrupt_type === 'hitl_approval'">
      <div class="interrupt-header">
        <svg class="interrupt-icon" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M12 4.2l8.4 14.6H3.6zM12 9.4v4.2M12 16.4v.2" />
        </svg>
        <span class="interrupt-title">需要人工确认以下操作</span>
      </div>

      <div class="interrupt-body">
        <div
          v-for="(action, idx) in interruptData.action_requests"
          :key="idx"
          class="action-card"
        >
          <div class="action-header">
            <span class="action-type">{{ formatActionName(action.name) }}</span>
          </div>
          <div class="action-details">
            <table class="order-table" v-if="action.args">
              <tbody>
                <template v-for="(val, key) in displayArgs(action.args)" :key="key">
                  <tr v-if="key !== 'orderDetail'">
                    <td class="field-label">{{ formatFieldName(key) }}</td>
                    <td class="field-value">{{ formatFieldValue(key, val) }}</td>
                  </tr>
                </template>
              </tbody>
            </table>
            <!-- 订单明细子表 -->
            <div v-if="action.args && action.args.orderDetail && action.args.orderDetail.length" class="detail-table-wrap">
              <div class="detail-label">订单明细 ({{ action.args.orderDetail.length }} 项)</div>
              <table class="detail-table">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>物料ID</th>
                    <th>数量</th>
                    <th>单价</th>
                    <th>小计</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(item, di) in action.args.orderDetail" :key="di">
                    <td>{{ di + 1 }}</td>
                    <td>{{ item.partId || '-' }}</td>
                    <td>{{ item.quantity || '-' }}</td>
                    <td>{{ item.unitPrice != null ? item.unitPrice : '-' }}</td>
                    <td>{{ item.subtotal || '-' }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>

      <div class="interrupt-actions">
        <button
          class="submit-btn approve-btn"
          @click="handleDecision('approve')"
          :disabled="submitting"
        >
          {{ submitting ? '处理中...' : '✓ 同意' }}
        </button>
        <button
          class="submit-btn reject-btn"
          @click="handleDecision('reject')"
          :disabled="submitting"
        >
          {{ submitting ? '处理中...' : '✗ 拒绝' }}
        </button>
      </div>
    </template>

    <!-- ============================================================ -->
    <!-- 未知中断类型（透传） -->
    <!-- ============================================================ -->
    <template v-else>
      <div class="interrupt-header">
        <svg class="interrupt-icon" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M9.4 5.6v12.8M14.6 5.6v12.8" />
        </svg>
        <span class="interrupt-title">Agent 已暂停，等待人工介入</span>
      </div>
      <div class="interrupt-body">
        <pre class="info-pre">{{ JSON.stringify(interruptData, null, 2) }}</pre>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'

const props = defineProps({
  interruptData: { type: Object, required: true },
  submitting: { type: Boolean, default: false }
})

const emit = defineEmits(['resume'])

const supplementText = ref('')
const supplementInputRef = ref(null)

// ============================================================
// 数据补充 — 提交
// ============================================================

function handleSupplement() {
  const text = supplementText.value.trim()
  if (!text || props.submitting) return
  emit('resume', { supplement: text })
}

/**
 * 补充输入框回车提交
 *
 * 中文输入法拼音选词时的回车不能当成提交，否则半截话就发出去了。
 * isComposing 是标准字段，keyCode 229 是部分旧浏览器/输入法的兜底。
 */
function handleSupplementEnter(event) {
  if (event.isComposing || event.keyCode === 229) return
  event.preventDefault()
  handleSupplement()
}

// ============================================================
// HITL 审批 — 提交决策
// ============================================================

function handleDecision(decisionType) {
  if (props.submitting) return
  emit('resume', { decisions: [{ type: decisionType }] })
}

// ============================================================
// 格式化辅助函数
// ============================================================

function formatCollectedData(data) {
  if (!data) return ''
  try {
    const parsed = typeof data === 'string' ? JSON.parse(data) : data
    return JSON.stringify(parsed, null, 2)
  } catch {
    return String(data)
  }
}

function formatActionName(name) {
  const map = {
    'order_create': '新建采购订单',
    'order_update': '修改采购订单'
  }
  return map[name] || name
}

function displayArgs(args) {
  // 过滤掉 orderDetail（单独展示），排除内部字段
  const skip = ['orderDetail']
  const result = {}
  for (const [k, v] of Object.entries(args)) {
    if (!skip.includes(k)) {
      result[k] = v
    }
  }
  return result
}

function formatFieldName(key) {
  const map = {
    'orderNumber': '订单编号',
    'totalAmount': '总金额',
    'status': '状态',
    'expectedDeliveryDate': '预计交货',
    'remark': '备注',
    'supplierId': '供应商ID',
    'supplierName': '供应商名称'
  }
  return map[key] || key
}

function formatFieldValue(key, val) {
  if (key === 'status') {
    const map = { 1: '待审核', 2: '已审核', 3: '采购中', 4: '已入库', 5: '已取消' }
    return map[val] != null ? map[val] : String(val)
  }
  if (val === null || val === undefined) return '-'
  return String(val)
}

// CSS class
// 必须用 computed：组件被复用（不同类型中断切换）时 bannerClass 要跟着变
const bannerClass = computed(() => ({
  'banner-supplement': props.interruptData.interrupt_type === 'order_info_supplement',
  'banner-approval': props.interruptData.interrupt_type === 'hitl_approval'
}))
</script>

<style scoped>
.interrupt-banner {
  flex-shrink: 0;
  max-width: 760px;
  width: calc(100% - 56px);
  margin: 4px auto 0;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--surface);
  animation: fade-in 0.2s var(--ease);
}

.banner-supplement {
  border-left: 2px solid var(--warn);
}

.banner-approval {
  border-left: 2px solid var(--danger);
}

/* ---------- 头部 ---------- */
.interrupt-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 13px 16px;
  border-bottom: 1px solid var(--border);
}

.interrupt-icon {
  width: 16px;
  height: 16px;
  flex-shrink: 0;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.banner-supplement .interrupt-icon {
  color: var(--warn);
}

.banner-approval .interrupt-icon {
  color: var(--danger);
}

.interrupt-title {
  font-size: 13px;
  font-weight: 500;
  color: var(--text);
}

/* ---------- 内容 ---------- */
.interrupt-body {
  padding: 14px 16px;
}

.info-block {
  margin-bottom: 12px;
}

.info-block:last-child {
  margin-bottom: 0;
}

.info-label {
  margin-bottom: 4px;
  font-size: 11px;
  color: var(--text-3);
}

.missing-fields .info-value {
  font-size: 13px;
  color: var(--warn);
}

.info-pre {
  margin: 0;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface-2);
  color: var(--text-2);
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.65;
  white-space: pre-wrap;
  overflow-x: auto;
}

/* ---------- 补充输入 ---------- */
.interrupt-input-area {
  display: flex;
  align-items: flex-end;
  gap: 8px;
  padding: 0 16px 10px;
}

.supplement-textarea {
  flex: 1;
  min-height: 58px;
  padding: 9px 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: transparent;
  color: var(--text);
  font-family: inherit;
  font-size: 13px;
  line-height: 1.65;
  resize: none;
  outline: none;
  transition: border-color var(--dur) var(--ease);
}

.supplement-textarea::placeholder {
  color: var(--text-3);
}

.supplement-textarea:focus {
  border-color: var(--border-strong);
}

.interrupt-hint {
  padding: 0 16px 12px;
  font-size: 11px;
  color: var(--text-3);
}

/* ---------- 审批卡 ---------- */
.action-card {
  margin-bottom: 10px;
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--surface-2);
}

.action-card:last-child {
  margin-bottom: 0;
}

.action-header {
  margin-bottom: 8px;
}

.action-type {
  font-size: 12.5px;
  font-weight: 500;
  color: var(--danger);
}

.order-table {
  width: 100%;
  border-collapse: collapse;
}

.order-table td {
  padding: 3px 0;
  font-size: 12.5px;
}

.field-label {
  width: 90px;
  color: var(--text-3);
}

.field-value {
  color: var(--text);
}

.detail-table-wrap {
  margin-top: 10px;
}

.detail-label {
  margin-bottom: 4px;
  font-size: 11px;
  color: var(--text-3);
}

.detail-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.detail-table th {
  padding: 6px 8px;
  border-bottom: 1px solid var(--border);
  text-align: left;
  font-weight: 500;
  color: var(--text-3);
  white-space: nowrap;
}

.detail-table td {
  padding: 6px 8px;
  border-bottom: 1px solid var(--border);
  color: var(--text-2);
}

.detail-table tr:last-child td {
  border-bottom: none;
}

/* ---------- 按钮 ---------- */
.submit-btn {
  height: 32px;
  padding: 0 16px;
  border: 1px solid transparent;
  border-radius: 8px;
  font-family: inherit;
  font-size: 12.5px;
  font-weight: 500;
  cursor: pointer;
  transition: background var(--dur) var(--ease), border-color var(--dur) var(--ease);
}

.submit-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.supplement-btn {
  flex-shrink: 0;
  background: var(--accent);
  color: var(--accent-ink);
}

.supplement-btn:hover:not(:disabled) {
  background: var(--brand-deep);
}

.approve-btn {
  background: var(--accent);
  color: var(--accent-ink);
}

.approve-btn:hover:not(:disabled) {
  background: var(--brand-deep);
}

.reject-btn {
  border-color: var(--border-strong);
  background: transparent;
  color: var(--text-2);
}

.reject-btn:hover:not(:disabled) {
  background: var(--surface-2);
  color: var(--text);
}

.interrupt-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 0 16px 14px;
}

@media (max-width: 720px) {
  .interrupt-banner {
    width: calc(100% - 32px);
  }

  .interrupt-input-area {
    flex-direction: column;
    align-items: stretch;
  }
}
</style>
