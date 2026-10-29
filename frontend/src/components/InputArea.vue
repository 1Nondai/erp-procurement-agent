<template>
  <div class="input-area">
    <div class="input-container">
      <!-- 工具条：左侧开关，右侧实时状态 -->
      <div class="input-toolbar">
        <label class="toggle-label">
          <input
            type="checkbox"
            :checked="showToolCalls"
            @change="emit('toggle-tool-calls', $event.target.checked)"
            class="toggle-checkbox"
          />
          <span class="toggle-switch"></span>
          <span class="toggle-text">显示工具调用</span>
        </label>

        <span class="stream-state" :class="{ live: streaming }">
          <span class="state-dot"></span>
          {{ streaming ? '正在生成' : '就绪' }}
        </span>
      </div>

      <!-- 输入坞：聚焦时出现渐变描边光晕 -->
      <div class="input-wrapper" :class="{ focused: isFocused, streaming: streaming }">
        <textarea
          ref="textareaRef"
          v-model="inputText"
          @focus="isFocused = true"
          @blur="isFocused = false"
          @keydown.enter.exact="handleEnter"
          @input="autoResize"
          placeholder="描述你的采购需求，例如：对比理化板与环氧树脂板台面的供应商报价和价差"
          rows="1"
          :disabled="streaming || disabled"
          class="input-textarea"
        ></textarea>

        <!-- 发送按钮：流式时切换为停止 -->
        <button
          v-if="!streaming"
          class="send-btn"
          @click="handleSend"
          :disabled="!inputText.trim() || disabled"
          title="发送（Enter）"
        >
          <span>发送</span>
          <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path d="M5 12h13M12 5l7 7-7 7" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
        </button>
        <button v-else class="stop-btn" @click="handleStop" title="停止生成">
          <span class="stop-icon" aria-hidden="true"></span>
          <span>停止</span>
        </button>
      </div>

      <!-- 提示信息 -->
      <div class="input-hint">
        <span v-if="!streaming">
          <kbd>Enter</kbd> 发送 · <kbd>Shift</kbd> + <kbd>Enter</kbd> 换行
        </span>
        <span v-else class="streaming-hint">
          <span class="wave" aria-hidden="true"><i></i><i></i><i></i></span>
          Agent 正在思考并调用工具…
        </span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, nextTick } from 'vue'

/**
 * 输入区域组件
 *
 * 提供消息输入功能：
 * - 正常状态：显示"发送"按钮
 * - 流式输出时：输入框禁用，按钮变为"停止"
 */

const props = defineProps({
  streaming: {
    type: Boolean,
    default: false
  },
  showToolCalls: {
    type: Boolean,
    default: true
  },
  // App 已经传了 :disabled="false"，这里显式声明，避免它落到根元素变成无意义的 HTML 属性
  disabled: {
    type: Boolean,
    default: false
  }
})

const emit = defineEmits(['send', 'stop', 'toggle-tool-calls'])

// 输入文本
const inputText = ref('')
// 是否获取焦点
const isFocused = ref(false)
// textarea 引用
const textareaRef = ref(null)

/**
 * 回车发送
 *
 * 中文输入法拼音选词时按回车不能当成发送，否则半截话就发出去了。
 * isComposing 是标准字段，keyCode 229 是部分旧浏览器/输入法的兜底。
 */
function handleEnter(e) {
  if (e.isComposing || e.keyCode === 229) return
  // 自己控制 preventDefault，避免在输入法组词阶段就吃掉回车
  e.preventDefault()
  handleSend()
}

/**
 * 发送消息
 */
function handleSend() {
  const text = inputText.value.trim()
  if (!text) return

  emit('send', text)
  inputText.value = ''

  nextTick(() => {
    if (textareaRef.value) {
      textareaRef.value.style.height = 'auto'
    }
  })
}

/**
 * 停止对话
 */
function handleStop() {
  emit('stop')
}

/**
 * 自动调整 textarea 高度
 */
function autoResize() {
  const textarea = textareaRef.value
  if (!textarea) return

  textarea.style.height = 'auto'
  textarea.style.height = Math.min(textarea.scrollHeight, 200) + 'px'
}
</script>

<style scoped>
.input-area {
  flex-shrink: 0;
  padding: 10px 28px 18px;
  background: var(--bg);
}

.input-container {
  max-width: 760px;
  margin: 0 auto;
}

/* ---------- 工具条 ---------- */
.input-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
  padding: 0 2px;
}

.toggle-label {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  cursor: pointer;
  user-select: none;
}

.toggle-checkbox {
  display: none;
}

.toggle-switch {
  position: relative;
  width: 26px;
  height: 15px;
  border-radius: 999px;
  background: var(--surface-3);
  transition: background var(--dur) var(--ease);
}

.toggle-switch::after {
  content: "";
  position: absolute;
  top: 2px;
  left: 2px;
  width: 11px;
  height: 11px;
  border-radius: 50%;
  background: var(--surface);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.15);
  transition: transform var(--dur) var(--ease);
}

.toggle-checkbox:checked + .toggle-switch {
  background: var(--accent);
}

.toggle-checkbox:checked + .toggle-switch::after {
  transform: translateX(11px);
}

.toggle-text {
  font-size: 11.5px;
  color: var(--text-3);
}

.toggle-label:hover .toggle-text {
  color: var(--text-2);
}

.stream-state {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  color: var(--text-3);
}

.state-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--border-strong);
}

.stream-state.live {
  color: var(--accent);
}

.stream-state.live .state-dot {
  background: var(--accent);
  animation: pulse-dot 1.4s var(--ease) infinite;
}

/* ---------- 输入框 ---------- */
.input-wrapper {
  display: flex;
  align-items: flex-end;
  gap: 10px;
  padding: 10px 10px 10px 14px;
  border: 1px solid var(--border-strong);
  border-radius: 12px;
  background: var(--surface);
  transition: border-color var(--dur) var(--ease), box-shadow var(--dur) var(--ease);
}

.input-wrapper.focused {
  border-color: var(--accent);
  box-shadow: var(--ring);
}

.input-wrapper.streaming {
  border-color: var(--border);
}

.input-textarea {
  flex: 1;
  min-height: 22px;
  max-height: 200px;
  border: none;
  outline: none;
  resize: none;
  background: transparent;
  color: var(--text);
  font-family: inherit;
  font-size: 14px;
  line-height: 1.7;
  padding: 2px 0;
}

.input-textarea::placeholder {
  color: var(--text-3);
}

.input-textarea:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

/* ---------- 发送 / 停止 ---------- */
.send-btn {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 32px;
  padding: 0 12px;
  border: none;
  border-radius: 8px;
  background: var(--accent);
  color: var(--accent-ink);
  font-family: inherit;
  font-size: 12.5px;
  font-weight: 500;
  cursor: pointer;
  transition: background var(--dur) var(--ease), opacity var(--dur) var(--ease);
}

.send-btn svg {
  width: 13px;
  height: 13px;
}

.send-btn:hover:not(:disabled) {
  background: var(--brand-deep);
}

.send-btn:disabled {
  background: var(--surface-3);
  color: var(--text-3);
  cursor: not-allowed;
}

.stop-btn {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 32px;
  padding: 0 12px;
  border: 1px solid var(--border-strong);
  border-radius: 8px;
  background: transparent;
  color: var(--text-2);
  font-family: inherit;
  font-size: 12.5px;
  font-weight: 500;
  cursor: pointer;
  transition: background var(--dur) var(--ease), border-color var(--dur) var(--ease);
}

.stop-btn:hover {
  background: var(--surface-2);
}

.stop-icon {
  width: 8px;
  height: 8px;
  border-radius: 1.5px;
  background: var(--text-3);
}

/* ---------- 提示 ---------- */
.input-hint {
  margin-top: 8px;
  text-align: center;
  font-size: 11px;
  color: var(--text-3);
}

.input-hint kbd {
  display: inline-block;
  padding: 0 4px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--surface-2);
  font-family: var(--font-mono);
  font-size: 10px;
  color: var(--text-3);
  line-height: 1.6;
}

.streaming-hint {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--text-2);
}

.wave {
  display: inline-flex;
  align-items: flex-end;
  gap: 2px;
  height: 9px;
}

.wave i {
  width: 2px;
  border-radius: 1px;
  background: var(--accent);
  animation: pulse-dot 1.2s var(--ease) infinite;
}

.wave i:nth-child(1) { height: 4px; }
.wave i:nth-child(2) { height: 8px; animation-delay: 0.14s; }
.wave i:nth-child(3) { height: 5px; animation-delay: 0.28s; }

@media (max-width: 720px) {
  .input-area {
    padding: 8px 16px 14px;
  }
}
</style>
