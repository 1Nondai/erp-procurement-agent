<template>
  <div class="app-container">
    <!-- 侧边栏 -->
    <Sidebar
      :sessions="sessions"
      :current-thread-id="currentThreadId"
      @select-session="handleSelectSession"
      @new-chat="handleNewChat"
      @delete-session="handleDeleteSession"
    />

    <!-- 主内容区 -->
    <main class="main-content">
      <!-- 顶栏：当前会话 + 运行状态，给页面一个稳定的产品框架 -->
      <header class="topbar">
        <div class="topbar-left">
          <span class="topbar-title">{{ currentTitle }}</span>
          <span class="status-pill" :class="{ live: isStreaming }">
            <i class="dot" aria-hidden="true"></i>
            {{ isStreaming ? '生成中' : '待命' }}
          </span>
        </div>
      </header>

      <!-- 对话区域 -->
      <ChatArea
        :messages="messages"
        :streaming="isStreaming"
        :show-tool-calls="showToolCalls"
        @send="handleSend"
      />

      <!-- ★ 人工介入中断横幅（数据补充 / HITL 审批） -->
      <InterruptBanner
        v-if="interruptData"
        :interrupt-data="interruptData"
        :submitting="isResuming"
        @resume="handleResume"
      />

      <!-- 输入区域：中断激活时隐藏，正常/流式时显示 -->
      <InputArea
        v-if="!interruptData"
        @send="handleSend"
        @stop="handleStop"
        @toggle-tool-calls="showToolCalls = $event"
        :disabled="false"
        :streaming="isStreaming"
        :show-tool-calls="showToolCalls"
      />
    </main>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import Sidebar from './components/Sidebar.vue'
import ChatArea from './components/ChatArea.vue'
import InputArea from './components/InputArea.vue'
import InterruptBanner from './components/InterruptBanner.vue'
import { streamChat, resumeChat } from './api/chat.js'
import { getSessions, getMessages, deleteSession } from './api/history.js'

/**
 * DeepAgent 聊天应用主组件
 *
 * 消息流按时间顺序混合展示：
 *   [user] → [assistant 文本] → [tool 调用] → [assistant 文本] → ...
 * 支持 Human-in-the-Loop 中断：
 *   ... → [interrupt] → [用户决策/补充] → [继续]
 */

// ============================================================
// 状态定义
// ============================================================

// 会话列表
const sessions = ref([])
// 当前会话 ID
const currentThreadId = ref(null)
// 消息列表（混合 user/assistant/tool，按时间顺序）
const messages = ref([])
// 是否正在流式输出
const isStreaming = ref(false)
// 是否正在处理中断恢复
const isResuming = ref(false)
// 是否显示工具调用
const showToolCalls = ref(true)
// 中断数据（null = 无中断，object = 有中断等待处理）
const interruptData = ref(null)
// AbortController 用于停止流式请求（只指向"当前最新"的请求）
let abortController = null
// 消息加载请求序号：用于丢弃过期响应（快速 A→B 切换时 A 的响应可能后到）
let loadMessagesReqId = 0

// 顶栏标题：跟随当前会话；新建对话（无选中）时显示"新对话"
const currentTitle = computed(() => {
  const current = sessions.value.find((s) => s.thread_id === currentThreadId.value)
  return current?.title || '新对话'
})

// ============================================================
// 生命周期
// ============================================================

/**
 * 组件挂载时加载会话列表
 */
onMounted(async () => {
  await loadSessions()
})

// ============================================================
// 会话管理
// ============================================================

/**
 * 加载会话列表
 */
async function loadSessions() {
  try {
    const response = await getSessions(1, 100)
    sessions.value = response.sessions || []
  } catch (error) {
    console.error('[App] 加载会话列表失败:', error)
    sessions.value = []
  }
}

/**
 * 选择会话
 */
async function handleSelectSession(threadId) {
  if (threadId === currentThreadId.value) return

  // 切会话前先停掉在途请求，否则旧请求的 token 会追加到新会话里
  stopActiveRequest()

  currentThreadId.value = threadId
  interruptData.value = null  // 切换会话时清除中断状态
  await loadMessages(threadId)
}

/**
 * 新建对话
 */
function handleNewChat() {
  // 如果有正在进行的请求，取消它
  stopActiveRequest()

  // 让在途的消息加载也失效，避免它把旧会话消息写回空列表
  loadMessagesReqId++

  interruptData.value = null
  currentThreadId.value = null
  messages.value = []
}

/**
 * 删除会话
 */
async function handleDeleteSession(threadId) {
  try {
    await deleteSession(threadId)
    await loadSessions()

    if (currentThreadId.value === threadId) {
      handleNewChat()
    }
  } catch (error) {
    console.error('[App] 删除会话失败:', error)
    alert('删除会话失败，请重试')
  }
}

/**
 * 加载会话消息
 */
async function loadMessages(threadId) {
  // 每次加载自增序号，响应回来时序号不是最新的就丢弃
  const reqId = ++loadMessagesReqId
  try {
    const response = await getMessages(threadId)
    // 过期响应（已切到别的会话 / 已新建对话）：直接丢弃，不要覆盖当前列表
    if (reqId !== loadMessagesReqId) return
    messages.value = response.messages || []
  } catch (error) {
    if (reqId !== loadMessagesReqId) return
    console.error('[App] 加载会话消息失败:', error)
    messages.value = []
  }
}

// ============================================================
// 停止对话
// ============================================================

/**
 * 停止在途请求并复位流式状态（切会话 / 新建对话 / 手动停止共用）
 */
function stopActiveRequest() {
  if (abortController) {
    abortController.abort()
    abortController = null
  }
  isStreaming.value = false
  isResuming.value = false
}

/**
 * 停止当前对话
 */
function handleStop() {
  if (abortController) {
    console.log('[App] 用户停止了对话')
    stopActiveRequest()
  }
}

// ============================================================
// 流式回调工厂 - 提取 handleSend 和 handleResume 共用的回调逻辑
// ============================================================

/**
 * 创建流式回调对象（消除 handleSend / handleResume 中的重复代码）
 *
 * @param {Object} options - 配置项
 * @param {boolean} options.isResume - 是否为恢复流程（影响 onDone 行为）
 * @param {string|null} options.threadId - 发起请求时的会话 ID，用于丢弃串会话的 token
 * @returns {Object} 回调对象
 */
function createStreamCallbacks({ isResume = false, threadId = null } = {}) {
  return {
    onToken: (content, source) => {
      // 会话已经切换：这是旧请求迟到的 token，丢掉，不要写进新会话
      if (threadId !== currentThreadId.value) return
      const lastMsg = messages.value[messages.value.length - 1]
      if (lastMsg && lastMsg.role === 'assistant') {
        lastMsg.content += content
        lastMsg.source = source
      } else {
        messages.value.push({
          id: `assistant-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
          role: 'assistant',
          content,
          source: source || 'main'
        })
      }
    },

    onToolStart: (tool) => {
      messages.value.push({
        id: tool.id,
        role: 'tool',
        tool_name: tool.name,
        args: '',
        text: '',
        images: [],
        source: tool.source || 'main',
        tool_status: 'calling'
      })
    },

    onToolArgs: (args) => {
      for (let i = messages.value.length - 1; i >= 0; i--) {
        const m = messages.value[i]
        if (m.role === 'tool' && m.tool_status === 'calling') {
          m.args += args
          break
        }
      }
    },

    onToolResult: (tool) => {
      for (let i = messages.value.length - 1; i >= 0; i--) {
        const m = messages.value[i]
        if (m.role === 'tool' && m.id === tool.id) {
          m.text = tool.text || ''
          m.images = tool.images || []
          m.tool_status = 'done'
          break
        }
      }
    },

    onToolEnd: (tool) => {
      if (tool) {
        for (let i = messages.value.length - 1; i >= 0; i--) {
          const m = messages.value[i]
          if (m.role === 'tool' && m.id === tool.id && m.tool_status === 'calling') {
            m.tool_status = 'done'
            break
          }
        }
      }
      // 兜底：将所有仍在 calling 的工具标记为 done
      for (const m of messages.value) {
        if (m.role === 'tool' && m.tool_status === 'calling') {
          m.tool_status = 'done'
        }
      }
    },

    onInterrupt: (data) => {
      console.log(`[App] ${isResume ? '恢复后再次' : ''}检测到中断:`, data.interrupt_type)
      // ★ 首轮就中断时，onDone 还没有机会写 thread_id，这里必须先落下，
      //   否则 handleResume 首行因 currentThreadId 为空直接 return，审批按钮点了没反应
      if (data.thread_id && !currentThreadId.value) {
        currentThreadId.value = data.thread_id
        loadSessions()
      }
      // 兜底：将所有 calling 状态的工具标记为 done
      for (const m of messages.value) {
        if (m.role === 'tool' && m.tool_status === 'calling') {
          m.tool_status = 'done'
        }
      }
      // 清理空的 assistant 消息
      messages.value = messages.value.filter(m => !(m.role === 'assistant' && !m.content))
      interruptData.value = data
    },

    onDone: (data) => {
      // 确保所有工具调用都标记为完成
      for (const m of messages.value) {
        if (m.role === 'tool' && m.tool_status === 'calling') {
          m.tool_status = 'done'
        }
      }
      // 清理空内容的 assistant 消息
      messages.value = messages.value.filter(m => {
        if (m.role === 'assistant' && !m.content && !data.aborted) return false
        return true
      })
      if (data.aborted) {
        const lastMsg = messages.value[messages.value.length - 1]
        if (lastMsg && lastMsg.role === 'assistant' && !lastMsg.content) {
          lastMsg.content = '（对话已停止）'
        }
      }
      if (isResume) {
        // 恢复流程：非中断完成时清除中断状态
        if (!data.interrupted) {
          interruptData.value = null
          if (data.thread_id && !currentThreadId.value) {
            currentThreadId.value = data.thread_id
            loadSessions()
          }
        }
      } else {
        // 发送流程：仅在非中断完成时更新 thread_id
        if (data.thread_id && !currentThreadId.value && !data.interrupted) {
          currentThreadId.value = data.thread_id
          loadSessions()
        }
      }
    },

    onError: (error) => {
      console.error(`[App] ${isResume ? '恢复对话' : '对话'}错误:`, error)
      if (isResume) {
        interruptData.value = null  // 出错时清除中断状态，恢复普通输入
      }
      const lastMsg = messages.value[messages.value.length - 1]
      const errorMsg = `抱歉，发生了错误：${error.message}`
      if (lastMsg && lastMsg.role === 'assistant' && !lastMsg.content) {
        lastMsg.content = errorMsg
      } else {
        messages.value.push({
          id: `assistant-${Date.now()}`,
          role: 'assistant',
          content: errorMsg,
          source: 'main'
        })
      }
    }
  }
}

// ============================================================
// 消息发送
// ============================================================

/**
 * 发送消息
 */
async function handleSend(message) {
  if (isStreaming.value) return

  // 添加用户消息
  messages.value.push({
    id: `user-${Date.now()}`,
    role: 'user',
    content: message
  })

  // 清除中断状态（如果有）
  interruptData.value = null

  // 设置流式状态
  isStreaming.value = true

  // 发起请求时的会话 ID：回调只在这个会话仍然激活时生效
  const boundThreadId = currentThreadId.value

  // 创建新的 AbortController（用局部变量，避免旧请求的 finally 清掉新请求的 controller）
  const controller = new AbortController()
  abortController = controller

  try {
    await streamChat(
      message,
      boundThreadId,
      createStreamCallbacks({ isResume: false, threadId: boundThreadId }),
      controller.signal
    )
  } catch (error) {
    if (error.name === 'AbortError') {
      console.log('[App] 请求已被取消')
    } else {
      console.error('[App] 发送消息失败:', error)
    }
  } finally {
    // 只有自己还是"当前请求"时才复位状态并清空 controller：
    // 旧请求的 finally 可能晚于新请求执行，无条件写会把新请求的流式状态/停止按钮一起抹掉
    if (abortController === controller) {
      isStreaming.value = false
      abortController = null
    }
  }
}

// ============================================================
// 中断恢复
// ============================================================

/**
 * 处理中断恢复（由 InterruptBanner 触发）
 *
 * @param {Object} resumeData - 恢复数据：
 *   - 数据补充: { supplement: "..." }
 *   - HITL 审批: { decisions: [{ type: "approve" }] }
 */
async function handleResume(resumeData) {
  if (isResuming.value || !currentThreadId.value) return

  isResuming.value = true

  // 发起请求时的会话 ID：回调只在这个会话仍然激活时生效
  const boundThreadId = currentThreadId.value

  // 创建新的 AbortController（局部变量，避免和旧请求互相清空）
  const controller = new AbortController()
  abortController = controller

  try {
    await resumeChat(
      boundThreadId,
      resumeData,
      createStreamCallbacks({ isResume: true, threadId: boundThreadId }),
      controller.signal
    )
  } catch (error) {
    if (error.name === 'AbortError') {
      console.log('[App] 恢复请求已被取消')
    } else {
      console.error('[App] 恢复请求失败:', error)
    }
  } finally {
    // 同上：旧请求的 finally 不能把新请求的状态清掉
    if (abortController === controller) {
      isResuming.value = false
      abortController = null
    }
  }
}
</script>

<style scoped>
.app-container {
  display: flex;
  height: 100vh;
  width: 100%;
  overflow: hidden;
  background: var(--bg);
}

.main-content {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  position: relative;
}

.topbar {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  height: 52px;
  padding: 0 28px;
  border-bottom: 1px solid var(--border);
  background: var(--bg);
}

.topbar-left {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}

.topbar-title {
  font-size: 13px;
  font-weight: 500;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 46vw;
}

/* 状态：只有一个小圆点 + 两个字，不做胶囊底色 */
.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  color: var(--text-3);
  white-space: nowrap;
}

.status-pill .dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--border-strong);
}

.status-pill.live {
  color: var(--accent);
}

.status-pill.live .dot {
  background: var(--accent);
  animation: pulse-dot 1.4s var(--ease) infinite;
}

@media (max-width: 720px) {
  .topbar {
    height: 48px;
    padding: 0 16px;
  }
}
</style>
