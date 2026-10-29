<template>
  <aside class="sidebar">
    <!-- 品牌区：logo 带渐变描边环，右侧是主题切换 -->
    <div class="brand">
      <!-- 品牌标：擎驰机车标识（SVG 矢量图形），
           放在 1px 描边容器里保持原色，深色主题下提亮一档保证可见 -->
      <div class="brand-mark" aria-hidden="true">
        <img :src="logoMark" alt="" />
      </div>
      <div class="brand-text">
        <span class="brand-name">擎驰机车</span>
        <span class="brand-sub">采购智能助手</span>
      </div>
      <button
        class="icon-btn theme-toggle"
        :title="theme === 'dark' ? '切换到浅色' : '切换到深色'"
        @click="toggleTheme"
      >
        <svg v-if="theme === 'dark'" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <circle cx="12" cy="12" r="4.2" stroke="currentColor" stroke-width="1.7" />
          <path
            d="M12 3v2.2M12 18.8V21M3 12h2.2M18.8 12H21M5.6 5.6l1.6 1.6M16.8 16.8l1.6 1.6M18.4 5.6l-1.6 1.6M7.2 16.8l-1.6 1.6"
            stroke="currentColor" stroke-width="1.7" stroke-linecap="round"
          />
        </svg>
        <svg v-else viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path
            d="M20 14.2A8 8 0 0 1 9.8 4 8.2 8.2 0 1 0 20 14.2Z"
            stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"
          />
        </svg>
      </button>
    </div>

    <!-- 新建对话 -->
    <button class="new-chat-btn" @click="$emit('new-chat')">
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
      </svg>
      <span>新建对话</span>
    </button>

    <!-- 搜索 -->
    <div class="search-box">
      <svg class="search-icon" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <circle cx="11" cy="11" r="6.4" stroke="currentColor" stroke-width="1.7" />
        <path d="m16 16 4 4" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" />
      </svg>
      <input
        type="text"
        v-model="searchKeyword"
        placeholder="搜索会话"
        class="search-input"
      />
      <button v-if="searchKeyword" class="search-clear" title="清空" @click="searchKeyword = ''">
        ×
      </button>
    </div>

    <!-- 会话列表 -->
    <div class="session-list">
      <div class="session-list-header">
        <span>历史会话</span>
        <span class="session-count">{{ filteredSessions.length }}</span>
      </div>

      <!-- 空状态：区分"没有会话"和"搜索无结果"，提示才有用 -->
      <div v-if="filteredSessions.length === 0" class="empty-state">
        <div class="empty-glyph">◇</div>
        <p>{{ sessions.length === 0 ? '还没有会话' : '没有匹配的会话' }}</p>
        <span class="hint">{{ sessions.length === 0 ? '点击上方「新建对话」开始' : '换个关键词试试' }}</span>
      </div>

      <div class="session-items">
        <div
          v-for="session in filteredSessions"
          :key="session.thread_id"
          class="session-item"
          :class="{ active: session.thread_id === currentThreadId }"
          @click="$emit('select-session', session.thread_id)"
        >
          <span class="session-indicator" aria-hidden="true"></span>
          <div class="session-info">
            <span class="session-title">{{ session.title }}</span>
            <span class="session-time">{{ formatTime(session.updated_at) }}</span>
          </div>
          <button
            class="delete-btn"
            @click.stop="handleDelete(session.thread_id)"
            title="删除会话"
          >
            <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path d="m6 6 12 12M18 6 6 18" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
            </svg>
          </button>
        </div>
      </div>
    </div>

    <!-- 底部：当前用户 + 版本 -->
    <div class="sidebar-footer">
      <div class="user-chip">
        <span class="user-avatar">{{ userInitials }}</span>
        <span class="user-meta">
          <span class="user-name">{{ userId }}</span>
          <span class="user-role">当前账号</span>
        </span>
      </div>
      <span class="version">v1.1</span>
    </div>
  </aside>
</template>

<script setup>
import { ref, computed } from 'vue'
import logoMark from '../assets/logo-mark.svg'
import { getUserId } from '../api/identity.js'

// Props 定义
const props = defineProps({
  sessions: {
    type: Array,
    default: () => []
  },
  currentThreadId: {
    type: String,
    default: null
  }
})

// Emits 定义
const emit = defineEmits(['select-session', 'new-chat', 'delete-session'])

// 搜索关键词
const searchKeyword = ref('')

// ---- 主题切换 ----
// 只改 <html data-theme> 一个属性，所有颜色都来自 CSS 变量，组件无需感知主题。
const theme = ref(
  document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark'
)

function toggleTheme() {
  theme.value = theme.value === 'dark' ? 'light' : 'dark'
  document.documentElement.setAttribute('data-theme', theme.value)
  try {
    localStorage.setItem('erp_theme', theme.value)
  } catch (e) {
    /* 隐私模式下 localStorage 可能不可用，忽略即可 */
  }
}

// ---- 当前用户 ----
// 来自 identity.js（与后端 X-User-Id 同一份来源），仅用于展示，不参与鉴权。
const userId = getUserId()
const userInitials = computed(() => (userId || 'U').slice(0, 2).toUpperCase())

// 过滤后的会话列表
const filteredSessions = computed(() => {
  if (!searchKeyword.value) {
    return props.sessions
  }
  return props.sessions.filter(session =>
    // title 可能为 null（空会话），直接 toLowerCase 会抛 TypeError 让整个侧边栏白屏
    (session.title || '').toLowerCase().includes(searchKeyword.value.toLowerCase())
  )
})

/**
 * 格式化时间
 */
function formatTime(timestamp) {
  if (!timestamp) return ''

  const date = new Date(timestamp)
  const now = new Date()
  const diff = now - date

  // 一天内显示相对时间
  if (diff < 24 * 60 * 60 * 1000) {
    const hours = Math.floor(diff / (60 * 60 * 1000))
    if (hours < 1) {
      const minutes = Math.floor(diff / (60 * 1000))
      return minutes < 1 ? '刚刚' : `${minutes} 分钟前`
    }
    return `${hours} 小时前`
  }

  // 超过一天显示日期
  return `${date.getMonth() + 1}/${date.getDate()}`
}

/**
 * 处理删除会话
 */
function handleDelete(threadId) {
  if (confirm('确定要删除这个会话吗？')) {
    emit('delete-session', threadId)
  }
}
</script>

<style scoped>
.sidebar {
  width: 264px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  padding: 18px 14px 14px;
  background: var(--surface);
  border-right: 1px solid var(--border);
}

/* ---------- 品牌 ---------- */
.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 4px 16px;
}

/* 标识保持原色（品牌蓝）放在浅底上：斜缝用底色透出，
   比"反白在蓝块里"更能保住图形轮廓 —— 小尺寸下不会糊成一个蝴蝶结 */
.brand-mark {
  width: 32px;
  height: 32px;
  display: grid;
  place-items: center;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface);
  flex-shrink: 0;
}

.brand-mark img {
  width: 22px;
  height: auto;
  display: block;
}

:root[data-theme="dark"] .brand-mark img {
  filter: brightness(1.22);
}

.brand-text {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
  flex: 1;
}

.brand-name {
  font-size: 13.5px;
  font-weight: 600;
  letter-spacing: 0.01em;
  color: var(--text);
}

.brand-sub {
  font-size: 11px;
  color: var(--text-3);
}

.icon-btn {
  width: 26px;
  height: 26px;
  display: grid;
  place-items: center;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--text-3);
  cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}

.icon-btn:hover {
  background: var(--surface-2);
  color: var(--text-2);
}

.icon-btn svg {
  width: 15px;
  height: 15px;
}

/* ---------- 新建对话 ---------- */
.new-chat-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  width: 100%;
  height: 36px;
  margin-bottom: 10px;
  border: 1px solid var(--border-strong);
  border-radius: 8px;
  background: transparent;
  color: var(--text);
  font-family: inherit;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: background var(--dur) var(--ease), border-color var(--dur) var(--ease);
}

.new-chat-btn:hover {
  background: var(--surface-2);
}

.new-chat-btn svg {
  width: 14px;
  height: 14px;
  color: var(--text-3);
}

/* ---------- 搜索 ---------- */
.search-box {
  display: flex;
  align-items: center;
  gap: 7px;
  height: 32px;
  padding: 0 9px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: transparent;
  transition: border-color var(--dur) var(--ease);
}

.search-box:focus-within {
  border-color: var(--border-strong);
}

.search-icon {
  width: 13px;
  height: 13px;
  color: var(--text-3);
  flex-shrink: 0;
}

.search-input {
  flex: 1;
  min-width: 0;
  border: none;
  outline: none;
  background: transparent;
  font-family: inherit;
  font-size: 12.5px;
  color: var(--text);
}

.search-input::placeholder {
  color: var(--text-3);
}

.search-clear {
  border: none;
  background: transparent;
  color: var(--text-3);
  font-size: 15px;
  line-height: 1;
  cursor: pointer;
  padding: 0 2px;
}

.search-clear:hover {
  color: var(--text-2);
}

/* ---------- 会话列表 ---------- */
.session-list {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  margin-top: 16px;
}

.session-list-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 6px 6px;
  font-size: 11px;
  color: var(--text-3);
  letter-spacing: 0.04em;
}

.session-count {
  font-variant-numeric: tabular-nums;
}

.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 3px;
  padding: 28px 12px;
  text-align: center;
}

.empty-glyph {
  font-size: 15px;
  color: var(--border-strong);
  margin-bottom: 4px;
}

.empty-state p {
  font-size: 12px;
  color: var(--text-2);
}

.empty-state .hint {
  font-size: 11px;
  color: var(--text-3);
}

.session-items {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.session-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 7px 6px 7px 8px;
  border-radius: 7px;
  cursor: pointer;
  transition: background var(--dur) var(--ease);
}

.session-item:hover {
  background: var(--surface-2);
}

.session-item.active {
  background: var(--surface-2);
}

.session-indicator {
  width: 2px;
  height: 12px;
  border-radius: 1px;
  background: transparent;
  flex-shrink: 0;
}

.session-item.active .session-indicator {
  background: var(--accent);
}

.session-info {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.session-title {
  font-size: 12.8px;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.session-item:not(.active) .session-title {
  color: var(--text-2);
}

.session-time {
  font-size: 10.5px;
  color: var(--text-3);
}

.delete-btn {
  width: 20px;
  height: 20px;
  display: grid;
  place-items: center;
  border: none;
  border-radius: 5px;
  background: transparent;
  color: var(--text-3);
  cursor: pointer;
  opacity: 0;
  transition: opacity var(--dur) var(--ease), background var(--dur) var(--ease);
}

.session-item:hover .delete-btn {
  opacity: 1;
}

.delete-btn:hover {
  background: var(--surface-3);
  color: var(--text-2);
}

.delete-btn svg {
  width: 11px;
  height: 11px;
}

/* ---------- 底部账号 ---------- */
.sidebar-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 12px 6px 0;
  border-top: 1px solid var(--border);
}

.user-chip {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.user-avatar {
  width: 24px;
  height: 24px;
  display: grid;
  place-items: center;
  border-radius: 50%;
  background: var(--surface-3);
  color: var(--text-2);
  font-size: 9.5px;
  font-weight: 600;
  letter-spacing: 0.02em;
}

.user-meta {
  display: flex;
  flex-direction: column;
  gap: 0;
  min-width: 0;
}

.user-name {
  font-size: 11.5px;
  color: var(--text-2);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.user-role {
  font-size: 10px;
  color: var(--text-3);
}

.version {
  font-size: 10px;
  color: var(--text-3);
}
</style>
