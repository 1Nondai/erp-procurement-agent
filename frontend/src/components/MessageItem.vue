<template>
  <!-- ============================================================ -->
  <!-- 用户消息 - 右侧蓝色气泡 -->
  <!-- ============================================================ -->
  <div v-if="message.role === 'user'" class="message-item message-user">
    <div class="message-content-wrapper">
      <div class="message-content">
        <div class="content-text">{{ message.content }}</div>
      </div>
    </div>
    <div class="message-avatar">
      <div class="avatar user-avatar" aria-hidden="true">
        <svg class="ico" viewBox="0 0 24 24">
          <circle cx="12" cy="8.4" r="3.4" />
          <path d="M5.4 19.4a6.9 6.9 0 0 1 13.2 0" />
        </svg>
      </div>
    </div>
  </div>

  <!-- ============================================================ -->
  <!-- AI 助手消息 - 左侧，带头部标签栏 -->
  <!-- ============================================================ -->
  <div v-else-if="message.role === 'assistant'" class="message-item message-assistant" :class="{ streaming: isStreaming }">
    <div class="message-avatar">
      <div class="avatar assistant-avatar" :class="getAvatarClass(message.source)" aria-hidden="true">
        <svg class="ico" viewBox="0 0 24 24">
          <path :d="getRoleIcon(message.source)" />
        </svg>
      </div>
    </div>
    <div class="message-content-wrapper">
      <!-- 角色标签栏 -->
      <div class="msg-label-bar" :class="getLabelBarClass(message.source)">
        <span class="msg-role-badge">{{ getRoleLabel(message.source) }}</span>
        <span v-if="message.source && message.source !== 'main'" class="msg-source-badge">
          {{ getSourceName(message.source) }}
        </span>
      </div>
      <!-- 消息正文 -->
      <div class="message-content">
        <MarkdownRenderer :content="message.content" />
        <span v-if="isStreaming && message.content" class="typing-cursor">▋</span>
      </div>
    </div>
  </div>

  <!-- ============================================================ -->
  <!-- 工具调用消息 - 左侧，带 Tool 标签栏 -->
  <!-- ============================================================ -->
  <div v-else-if="message.role === 'tool'" class="message-item message-tool-inline">
    <div class="message-avatar">
      <div class="avatar tool-avatar" aria-hidden="true">
        <svg class="ico" viewBox="0 0 24 24">
          <path :d="getToolIcon(message.tool_name)" />
        </svg>
      </div>
    </div>
    <div class="message-content-wrapper">
      <!-- Tool 标签栏 -->
      <div class="msg-label-bar tool-label-bar">
        <span class="msg-role-badge tool-role-badge">Tool</span>
        <span class="msg-tool-name-badge">{{ formatToolName(message.tool_name) }}</span>
        <span v-if="message.source && message.source !== 'main'" class="msg-source-badge tool-source-badge">
          {{ getSourceName(message.source) }}
        </span>
        <span v-if="message.tool_status === 'calling' && !hasToolContent" class="tool-status-badge calling">执行中</span>
        <span v-else-if="hasToolContent" class="tool-status-badge done">完成</span>
      </div>

      <!-- 参数（可折叠） -->
      <div v-if="message.args" class="tool-inline-section">
        <div class="tool-section-header" @click="toggleSection('args')">
          <span class="section-arrow">{{ expandedSections.includes('args') ? '▼' : '▶' }}</span>
          <span class="section-label">参数</span>
        </div>
        <pre v-if="expandedSections.includes('args')" class="tool-inline-code">{{ formatArgs(message.args) }}</pre>
      </div>

      <!-- 结果文本（可折叠） -->
      <div v-if="message.text" class="tool-inline-section">
        <div class="tool-section-header" @click="toggleSection('result')">
          <span class="section-arrow">{{ expandedSections.includes('result') ? '▼' : '▶' }}</span>
          <span class="section-label">结果</span>
        </div>
        <div v-if="expandedSections.includes('result')" class="tool-inline-result">
          <MarkdownRenderer :content="cleanText(message.text)" />
        </div>
      </div>

      <!-- 结果图片 -->
      <div v-if="message.images && message.images.length > 0" class="tool-inline-images">
        <div
          v-for="(img, imgIdx) in message.images"
          :key="imgIdx"
          class="tool-image-wrapper"
        >
          <img
            :src="img"
            :alt="`图表 ${imgIdx + 1}`"
            class="tool-result-image"
            loading="lazy"
            style="max-width:100%;max-height:360px;width:auto;height:auto;object-fit:contain;border-radius:8px;cursor:pointer;"
            @click="previewImage(img)"
          />
        </div>
      </div>

      <!-- 等待中动画 -->
      <div v-if="message.tool_status === 'calling' && !hasToolContent" class="tool-waiting">
        <span class="waiting-dot"></span>
        <span class="waiting-dot"></span>
        <span class="waiting-dot"></span>
        <span class="waiting-text">执行中...</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import MarkdownRenderer from './MarkdownRenderer.vue'

const props = defineProps({
  message: { type: Object, required: true },
  isStreaming: { type: Boolean, default: false }
})

const expandedSections = ref(['result'])

const hasToolContent = computed(() => {
  return !!(props.message.text || (props.message.images && props.message.images.length > 0))
})

function toggleSection(name) {
  const idx = expandedSections.value.indexOf(name)
  if (idx === -1) {
    expandedSections.value.push(name)
  } else {
    expandedSections.value.splice(idx, 1)
  }
}

function previewImage(src) {
  window.open(src, '_blank')
}

// ============================================================
// 常量映射表
// ============================================================

/** 工具名称中文映射 */
const TOOL_NAME_MAP = {
  task: '委派子代理 (task)',
  generate_column_chart: '生成柱状图',
  generate_bar_chart: '生成柱状图',
  generate_line_chart: '生成折线图',
  generate_pie_chart: '生成饼图',
  generate_area_chart: '生成面积图',
  generate_scatter_chart: '生成散点图',
  web_search: '网络搜索',
  read_file: '读取文件',
  write_file: '写入文件',
  execute_python: '执行 Python 代码'
}

/** 来源名称中文映射 */
const SOURCE_NAME_MAP = {
  main: '主助手',
  'chart-agent': '图表代理',
  researcher: '研究代理',
  'model-agent': '模型代理',
  general: '通用代理'
}

// ============================================================
// AI 消息标签
// ============================================================

/** AI 消息的角色标签文字 */
function getRoleLabel(source) {
  // 主 Agent 标 "Agent"；子 Agent 标 "Sub Agent"，
  // 具体名字交给右侧的 source 徽标，避免同一个名字出现两次
  return (!source || source === 'main') ? 'Agent' : 'Sub Agent'
}

// ============================================================
// 图标：用 SVG path 而不是 emoji
// 原因：emoji 依赖系统字体，在部分 Windows/Linux 环境会退化成
// 单色字形甚至豆腐块，且无法跟随主题色。SVG 用 currentColor 描边，
// 天然适配深浅两套主题。
// ============================================================

const ICON_PATHS = {
  // 主 Agent：星芒，代表"生成/思考"
  spark: 'M12 3.4l1.85 4.95 4.95 1.85-4.95 1.85L12 17l-1.85-4.95L5.2 10.2l4.95-1.85z',
  // 子 Agent：层叠，代表"被编排的下级单元"
  layers: 'M12 4.2l8 3.9-8 3.9-8-3.9zM4 12.6l8 3.9 8-3.9M4 16.9l8 3.9 8-3.9',
  // 工具：尖括号，代表"执行/调用"
  code: 'M9.2 7.6L4.4 12l4.8 4.4M14.8 7.6L19.6 12l-4.8 4.4',
  // 图表类工具
  chart: 'M4 19.6V5M4 19.6h16M8.2 19.6v-5.4M12.4 19.6V9.2M16.6 19.6v-3.6',
  // 检索类工具
  search: 'M10.8 4.6a6.2 6.2 0 1 0 0 12.4 6.2 6.2 0 0 0 0-12.4zM15.4 15.4l3.9 3.9',
  // 文档类工具
  book: 'M4.6 5.6h5.6a2 2 0 0 1 2 2v11a2 2 0 0 0-2-2H4.6zM19.4 5.6h-5.6a2 2 0 0 0-2 2v11a2 2 0 0 1 2-2h5.6z'
}

/** AI 消息的头像图标（主/子 Agent 用不同语义） */
function getRoleIcon(source) {
  return (!source || source === 'main') ? ICON_PATHS.spark : ICON_PATHS.layers
}

/** AI 消息标签栏的 CSS class */
function getLabelBarClass(source) {
  return (!source || source === 'main') ? 'label-bar-main' : 'label-bar-sub'
}

/** AI 消息头像的 CSS class */
function getAvatarClass(source) {
  return (!source || source === 'main') ? 'avatar-main' : 'avatar-sub'
}

// ============================================================
// 工具相关
// ============================================================

function formatToolName(name) {
  if (!name) return '未知工具'
  return TOOL_NAME_MAP[name] || name
}

function getSourceName(source) {
  if (!source) return '主助手'
  // 未登记的 source 也做可读化：chart-agent → chart agent
  return SOURCE_NAME_MAP[source] || String(source).replace(/[-_]/g, ' ')
}

function getToolIcon(name) {
  const n = (name || '').toLowerCase()
  if (n.includes('chart') || n.includes('generate_') || n.includes('fenxi')) return ICON_PATHS.chart
  if (n.includes('search') || n.includes('web') || n.includes('research')) return ICON_PATHS.search
  if (n.includes('read') || n.includes('write')) return ICON_PATHS.book
  return ICON_PATHS.code
}

function formatArgs(args) {
  if (!args) return ''
  try {
    return JSON.stringify(JSON.parse(args), null, 2)
  } catch {
    return args
  }
}

function cleanText(text) {
  if (!text) return ''
  // 移除工具返回结果中的内部 id 字段
  return text
    .replace(/,?\s*'id':\s*'[^']*'/g, '')
    .replace(/,?\s*"id":\s*"[^"]*"/g, '')
}
</script>

<style scoped>
.message-item {
  display: flex;
  gap: 10px;
  padding: 12px 28px;
  max-width: 820px;
  margin: 0 auto;
  animation: fade-in 0.2s var(--ease) both;
}

.message-avatar {
  flex-shrink: 0;
  padding-top: 1px;
}

.avatar {
  width: 26px;
  height: 26px;
  display: grid;
  place-items: center;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--surface);
  color: var(--text-3);
}

.avatar .ico {
  width: 14px;
  height: 14px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.5;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.avatar-main {
  border-color: var(--accent-line);
  background: var(--accent-soft);
  color: var(--accent);
}

.avatar-sub {
  background: var(--surface-2);
  color: var(--text-3);
}

.tool-avatar {
  border-color: var(--warn-line);
  background: var(--warn-soft);
  color: var(--warn);
}

/* ---------- 用户 ---------- */
.message-user {
  justify-content: flex-end;
}

.message-user .message-content-wrapper {
  max-width: min(560px, 78%);
}

.message-user .message-content {
  padding: 9px 13px;
  border-radius: 12px;
  background: var(--bubble-user);
}

.message-user .content-text {
  font-size: 13.8px;
  line-height: 1.75;
  color: var(--text);
  white-space: pre-wrap;
  word-break: break-word;
}

/* ---------- 助手 ---------- */
.message-assistant .message-content-wrapper {
  width: 100%;
  min-width: 0;
  overflow: hidden;
}

.message-assistant .message-content {
  padding-top: 2px;
  font-size: 14px;
  line-height: 1.85;
  color: var(--text);
  overflow: hidden;
}

/* 标签：只用灰色小字，不做彩色胶囊 */
.msg-label-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 3px;
  flex-wrap: wrap;
  font-size: 11px;
  color: var(--text-3);
}

.msg-role-badge {
  font-size: 11px;
  color: var(--text-3);
}

.label-bar-main .msg-role-badge {
  color: var(--text-2);
  font-weight: 500;
}

.msg-source-badge::before {
  content: "· ";
}

.msg-tool-name-badge {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-2);
}

.tool-status-badge {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-3);
}

.tool-status-badge.calling {
  color: var(--warn);
}

.tool-status-badge.done {
  color: var(--ok);
}

/* ---------- 工具卡 ---------- */
.message-tool-inline .message-content-wrapper {
  width: 100%;
  min-width: 0;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface);
  overflow: hidden;
}

.tool-inline-section {
  margin-top: 6px;
}

.tool-section-header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 3px 0;
  font-size: 11.5px;
  color: var(--text-3);
  cursor: pointer;
  user-select: none;
  transition: color var(--dur) var(--ease);
}

.tool-section-header:hover {
  color: var(--text-2);
}

.section-arrow {
  width: 10px;
  font-size: 8px;
}

.tool-inline-code {
  margin: 6px 0 0 16px;
  padding: 10px 12px;
  max-height: 200px;
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface-2);
  color: var(--text-2);
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-all;
}

.tool-inline-result {
  margin: 6px 0 0 16px;
  font-size: 13px;
  line-height: 1.7;
  color: var(--text-2);
  overflow: hidden;
}

.tool-inline-images {
  margin-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.tool-image-wrapper {
  display: flex;
  justify-content: center;
  padding: 6px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: #ffffff;
}

.tool-result-image {
  max-width: 100% !important;
  max-height: 340px;
  width: auto;
  height: auto;
  display: block;
  border-radius: 4px;
  cursor: zoom-in;
  object-fit: contain;
}

.tool-waiting {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 8px 0 2px;
}

.waiting-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--text-3);
  animation: pulse-dot 1.2s var(--ease) infinite;
}

.waiting-dot:nth-child(2) { animation-delay: 0.15s; }
.waiting-dot:nth-child(3) { animation-delay: 0.3s; }

.waiting-text {
  margin-left: 4px;
  font-size: 11.5px;
  color: var(--text-3);
}

.typing-cursor {
  display: inline-block;
  width: 1.5px;
  height: 1em;
  margin-left: 2px;
  vertical-align: -2px;
  background: var(--text-3);
  animation: pulse-dot 1s infinite;
}

@media (max-width: 720px) {
  .message-item {
    padding: 10px 16px;
  }
}
</style>
