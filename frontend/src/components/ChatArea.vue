<template>
  <div class="chat-area">
    <!-- 消息列表 -->
    <div class="message-list" ref="messageListRef">
      <!-- 空状态：极简三件套——标识、一句话、快捷入口 -->
      <div v-if="displayMessages.length === 0" class="empty-state">
        <div class="empty-inner">
          <div class="empty-top-row">
            <div class="empty-logo-icon" aria-hidden="true">
              <img :src="logoMark" alt="" />
            </div>
          </div>

          <h2>把采购需求，<span class="grad-text">说给它听</span></h2>
          <p class="empty-sub">
            比价、查料、看库存、下单改单，一句话交给 Agent。
          </p>

          <div class="suggestion-grid">
            <button
              v-for="item in suggestions"
              :key="item.title"
              class="suggestion"
              @click="$emit('send', item.prompt)"
            >
              <span class="suggestion-icon" aria-hidden="true">
                <svg class="s-ico" viewBox="0 0 24 24">
                  <path :d="item.icon" />
                </svg>
              </span>
              <span class="suggestion-text">
                <span class="suggestion-title">{{ item.title }}</span>
                <span class="suggestion-desc">{{ item.desc }}</span>
              </span>
              <span class="suggestion-arrow" aria-hidden="true">→</span>
            </button>
          </div>
        </div>
      </div>

      <!-- 消息列表（user / assistant / tool 按时间顺序混合展示）-->
      <div v-else class="messages">
        <MessageItem
          v-for="(message, index) in displayMessages"
          :key="message.id || index"
          :message="message"
          :is-streaming="isStreamingForMessage(message)"
        />
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, watch, nextTick } from 'vue'
import MessageItem from './MessageItem.vue'
import logoMark from '../assets/logo-mark.svg'

/**
 * 对话区域组件
 *
 * 显示消息列表（user / assistant / tool 按时间顺序混合展示）
 * 当 showToolCalls 为 false 时，过滤掉 tool 消息只显示 AI 回复
 */

const props = defineProps({
  messages: {
    type: Array,
    default: () => []
  },
  streaming: {
    type: Boolean,
    default: false
  },
  showToolCalls: {
    type: Boolean,
    default: true
  }
})

// 空状态快捷任务：点击直接把预设 prompt 发出去（对应真实可用的业务工具）
const emit = defineEmits(['send'])

// 快捷任务图标用 SVG path（emoji 依赖系统字体，跨环境渲染不一致）
const ICONS = {
  scale: 'M12 4.6v14.8M6.4 19.4h11.2M4.4 9h15.2M4.4 9l-1.9 3.9a2.4 2.4 0 0 0 3.8 0zM19.6 9l1.9 3.9a2.4 2.4 0 0 1-3.8 0z',
  box: 'M4.6 8.4l7.4-3.9 7.4 3.9v7.2L12 19.5l-7.4-3.9zM4.6 8.4L12 12.3l7.4-3.9M12 12.3v7.2',
  trend: 'M4 17.6l4.6-4.8 3.4 3 6.6-7M14.6 8.4h4.4v4.4',
  receipt: 'M6.4 4.6h11.2v14.8l-2.8-1.7-2.8 1.7-2.8-1.7-2.8 1.7zM9.2 9h5.6M9.2 12.4h5.6'
}

const suggestions = [
  {
    icon: ICONS.scale,
    title: '台面比价',
    desc: '理化板 vs 环氧树脂板',
    prompt: '对比理化板台面与环氧树脂板台面在各供应商的报价，看最低价是谁、价差有多大'
  },
  {
    icon: ICONS.box,
    title: '低库存补货',
    desc: '按缺口与提前期排序',
    prompt: '查一下当前库存低于安全库存的物料，按缺口大小和采购提前期排出补货优先级'
  },
  {
    icon: ICONS.receipt,
    title: '项目用料明细',
    desc: 'LHK-2026-001 已采购',
    prompt: '查一下 LHK-2026-001 项目已经采购了哪些材料，明细和金额合计是多少'
  },
  {
    icon: ICONS.trend,
    title: '供应商评估',
    desc: 'QSDC 评分选 A 级',
    prompt: '按 QSDC 评分帮我推荐 A 级信用的台面和钢材供应商，说明各自的交付和账期情况'
  }
]

// 根据开关过滤展示的消息
const displayMessages = computed(() => {
  if (props.showToolCalls) {
    return props.messages
  }
  // 过滤掉 tool 消息，只保留 user 和 assistant
  return props.messages.filter(m => m.role !== 'tool')
})

// 消息列表引用
const messageListRef = ref(null)

/**
 * 判断当前消息是否处于流式输出状态
 * 只有最后一条 assistant 消息在 streaming 时才显示光标
 */
function isStreamingForMessage(msg) {
  if (!props.streaming || msg.role !== 'assistant') return false
  // 找到 messages 中最后一条 assistant 消息
  const lastAssistant = [...props.messages].reverse().find(m => m.role === 'assistant')
  return lastAssistant === msg
}

// 监听消息变化，自动滚动到底部
watch(
  () => props.messages.length,
  () => {
    nextTick(() => {
      scrollToBottom()
    })
  }
)

// 深度监听消息内容变化（流式更新时也会触发滚动）
watch(
  () => props.messages,
  () => {
    nextTick(() => {
      scrollToBottom()
    })
  },
  { deep: true }
)

/**
 * 滚动到底部
 */
function scrollToBottom() {
  if (messageListRef.value) {
    messageListRef.value.scrollTop = messageListRef.value.scrollHeight
  }
}
</script>

<style scoped>
.chat-area {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.message-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
  scroll-behavior: smooth;
}

/* ---------- 空状态 ---------- */
.empty-state {
  height: 100%;
  display: grid;
  place-items: center;
  padding: 40px 28px;
}

.empty-inner {
  width: 100%;
  max-width: 600px;
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  animation: fade-in 0.3s var(--ease) both;
}

.empty-top-row {
  margin-bottom: 22px;
}

/* 主视觉只留标识本身，不加容器——极简，也避免小尺寸丢细节 */
.empty-logo-icon {
  display: grid;
  place-items: center;
}

.empty-logo-icon img {
  width: 46px;
  height: auto;
  display: block;
}

:root[data-theme="dark"] .empty-logo-icon img {
  filter: brightness(1.22);
}

.empty-state h2 {
  font-size: 26px;
  font-weight: 600;
  letter-spacing: -0.015em;
  line-height: 1.45;
  color: var(--text);
  margin-bottom: 10px;
}

.empty-sub {
  max-width: 430px;
  font-size: 13px;
  line-height: 1.8;
  color: var(--text-3);
  margin-bottom: 32px;
}

.suggestion-grid {
  width: 100%;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}

.suggestion {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--surface);
  color: var(--text);
  font-family: inherit;
  text-align: left;
  cursor: pointer;
  transition: border-color var(--dur) var(--ease), background var(--dur) var(--ease);
}

.suggestion:hover {
  border-color: var(--border-strong);
  background: var(--surface-2);
}

.suggestion-icon {
  display: grid;
  place-items: center;
  color: var(--text-3);
  flex-shrink: 0;
}

.suggestion:hover .suggestion-icon {
  color: var(--accent);
}

.suggestion-icon .s-ico {
  width: 16px;
  height: 16px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.suggestion-text {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
  flex: 1;
}

.suggestion-title {
  font-size: 12.8px;
  font-weight: 500;
  color: var(--text);
}

.suggestion-desc {
  font-size: 11px;
  color: var(--text-3);
}

.suggestion-arrow {
  font-size: 12px;
  color: var(--text-3);
  opacity: 0;
  transition: opacity var(--dur) var(--ease);
}

.suggestion:hover .suggestion-arrow {
  opacity: 1;
}

.messages {
  padding: 26px 0 10px;
}

@media (max-width: 720px) {
  .suggestion-grid {
    grid-template-columns: 1fr;
  }

  .empty-state {
    padding: 28px 18px;
  }

  .empty-state h2 {
    font-size: 22px;
  }
}

/* 矮窗口：压缩节奏，保证四张卡都在首屏 */
@media (max-height: 760px) {
  .empty-state {
    padding: 20px 24px;
  }

  .empty-logo-icon {
    width: 44px;
    height: 44px;
    border-radius: 11px;
  }

  .empty-logo-icon img {
    width: 24px;
  }

  .empty-top-row {
    margin-bottom: 14px;
  }

  .empty-state h2 {
    font-size: 22px;
    margin-bottom: 8px;
  }

  .empty-sub {
    margin-bottom: 20px;
  }

  .suggestion {
    padding: 9px 12px;
  }
}
</style>
