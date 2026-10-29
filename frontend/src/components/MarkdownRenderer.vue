<template>
  <div class="markdown-renderer" v-html="renderedContent"></div>
</template>

<script>
import MarkdownIt from 'markdown-it'
// 只引 core + 按需注册语言；整包 import 会把全部语法打进产物，体积和首屏解析都吃亏
import hljs from 'highlight.js/lib/core'
import javascript from 'highlight.js/lib/languages/javascript'
import typescript from 'highlight.js/lib/languages/typescript'
import python from 'highlight.js/lib/languages/python'
import json from 'highlight.js/lib/languages/json'
import bash from 'highlight.js/lib/languages/bash'
import sql from 'highlight.js/lib/languages/sql'
import java from 'highlight.js/lib/languages/java'
import xml from 'highlight.js/lib/languages/xml'
import css from 'highlight.js/lib/languages/css'

/**
 * Markdown 渲染组件（渲染器部分）
 *
 * 图片样式采用内联 style 方式注入到 img 标签中，
 * 因为 v-html 渲染的内容不受 Vue scoped CSS 影响。
 */

// js/ts/py/sh/html 等别名由各语言定义自带，无需重复注册；
// 未注册的语言 highlight 返回空串，代码块退化为纯文本，不影响展示
hljs.registerLanguage('javascript', javascript)
hljs.registerLanguage('typescript', typescript)
hljs.registerLanguage('python', python)
hljs.registerLanguage('json', json)
hljs.registerLanguage('bash', bash)
hljs.registerLanguage('sql', sql)
hljs.registerLanguage('java', java)
hljs.registerLanguage('xml', xml)
hljs.registerLanguage('css', css)

// 图片内联样式（确保在任何容器中都能正确缩放）
const IMG_INLINE_STYLE = 'max-width:100%;max-height:360px;width:auto;height:auto;object-fit:contain;display:block;border-radius:12px;margin:14px 0;box-shadow:0 2px 8px rgba(0,0,0,0.08);'

function isKnownImageHost(url) {
  const imageHosts = [
    'mdn.alipayobjects.com',
    'img.alicdn.com',
    'gw.alipayobjects.com',
    'zos.alipayobjects.com',
  ]
  try {
    const hostname = new URL(url).hostname
    return imageHosts.some(h => hostname.includes(h))
  } catch {
    return false
  }
}

function shouldRenderAsImage(src) {
  if (/\.(png|jpg|jpeg|gif|webp|svg|bmp)(\?.*)?$/i.test(src)) return true
  if (/^data:image\//i.test(src)) return true
  if (isKnownImageHost(src)) return true
  return false
}

function buildImgTag(src, alt) {
  return `<img src="${src}" alt="${alt || '图片'}" class="markdown-image" loading="lazy" style="${IMG_INLINE_STYLE}" />`
}

// markdown-it 单例：渲染器配置和组件生命周期无关，
// 之前放在 onMounted 里按组件新建，50 条消息就是 50 个实例
let md = null

function getMarkdown() {
  if (md) return md

  md = new MarkdownIt({
    // ★ 关闭原生 HTML：工具结果可能夹带外部网页片段，v-html 直出会 XSS。
    // 表格和代码高亮走的是 markdown 语法与渲染器，不依赖这个开关
    html: false,
    xhtmlOut: true,
    breaks: true,
    linkify: true,
    typographer: true,
    highlight: function (str, lang) {
      if (lang && hljs.getLanguage(lang)) {
        try {
          return hljs.highlight(str, { language: lang }).value
        } catch (__) {}
      }
      return ''
    }
  })

  md.renderer.rules.image = function (tokens, idx, options, env, self) {
    const token = tokens[idx]
    const src = token.attrs[token.attrIndex('src')][1]
    const alt = token.content

    if (shouldRenderAsImage(src)) {
      return buildImgTag(src, alt)
    }

    return `<a href="${src}" target="_blank" rel="noopener noreferrer" class="image-link">🖼️ ${alt || src}</a>`
  }

  // 渲染出的链接统一新窗口打开，并断开 opener 引用（防 reverse tabnabbing）
  const defaultLinkOpen = md.renderer.rules.link_open || function (tokens, idx, options, env, self) {
    return self.renderToken(tokens, idx, options)
  }

  md.renderer.rules.link_open = function (tokens, idx, options, env, self) {
    const token = tokens[idx]
    token.attrSet('target', '_blank')
    token.attrSet('rel', 'noopener noreferrer')
    return defaultLinkOpen(tokens, idx, options, env, self)
  }

  const defaultTextRenderer = md.renderer.rules.text || function (tokens, idx) {
    return md.utils.escapeHtml(tokens[idx].content)
  }

  md.renderer.rules.text = function (tokens, idx, options, env, self) {
    const content = tokens[idx].content
    // 检测 data:image base64
    if (/data:image\/[^;]+;base64,/.test(content)) {
      const imgMatch = content.match(/(data:image\/[^;]+;base64,[A-Za-z0-9+/=]+)/)
      if (imgMatch) {
        return buildImgTag(imgMatch[1], '图片')
      }
    }
    // 检测图片 CDN 域名
    for (const host of ['mdn.alipayobjects.com', 'img.alicdn.com', 'gw.alipayobjects.com']) {
      const regex = new RegExp(`(https?://[^\\s]*${host.replace(/\./g, '\\.')}[^\\s]*)`)
      const match = content.match(regex)
      if (match) {
        return buildImgTag(match[1], '图片')
      }
    }
    return defaultTextRenderer(tokens, idx, options, env, self)
  }

  return md
}
</script>

<script setup>
import { ref, watch } from 'vue'

const props = defineProps({
  content: {
    type: String,
    default: ''
  }
})

const renderedContent = ref('')

function renderContent() {
  if (!props.content) {
    renderedContent.value = escapeHtml(props.content || '')
    return
  }

  try {
    renderedContent.value = getMarkdown().render(props.content)
  } catch (error) {
    console.error('[MarkdownRenderer] 渲染失败:', error)
    renderedContent.value = escapeHtml(props.content)
  }
}

function escapeHtml(text) {
  const div = document.createElement('div')
  div.textContent = text
  return div.innerHTML
}

watch(() => props.content, () => {
  renderContent()
})

// 首屏直接渲染一次（md 单例在 getMarkdown 里懒创建，不再依赖 onMounted）
renderContent()
</script>

/* scoped：仅作用组件根元素 */
<style scoped>
.markdown-renderer {
  font-size: 14px;
  line-height: 1.85;
  color: var(--text);
  word-break: break-word;
  overflow-wrap: break-word;
  overflow: hidden;
}
</style>

<!-- 非 scoped：作用于 v-html 渲染出的元素 -->
<style>
.markdown-renderer h1 {
  font-size: 20px;
  font-weight: 600;
  letter-spacing: -0.01em;
  margin: 26px 0 12px;
  color: var(--text);
}

.markdown-renderer h2 {
  font-size: 16.5px;
  font-weight: 600;
  margin: 22px 0 10px;
  color: var(--text);
}

.markdown-renderer h3 {
  font-size: 15px;
  font-weight: 600;
  margin: 18px 0 8px;
  color: var(--text);
}

.markdown-renderer h4,
.markdown-renderer h5,
.markdown-renderer h6 {
  font-size: 13.5px;
  font-weight: 600;
  margin: 14px 0 6px;
  color: var(--text-2);
}

.markdown-renderer h1:first-child,
.markdown-renderer h2:first-child,
.markdown-renderer h3:first-child {
  margin-top: 2px;
}

.markdown-renderer p {
  margin: 9px 0;
}

.markdown-renderer ul,
.markdown-renderer ol {
  margin: 9px 0;
  padding-left: 20px;
}

.markdown-renderer li {
  margin: 4px 0;
}

.markdown-renderer li::marker {
  color: var(--text-3);
}

.markdown-renderer strong {
  font-weight: 600;
  color: var(--text);
}

/* 行内代码：浅底无边框，像纸面上的荧光笔 */
.markdown-renderer code {
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--surface-2);
  color: var(--text-2);
  font-family: var(--font-mono);
  font-size: 0.9em;
}

.markdown-renderer pre {
  margin: 12px 0;
  padding: 14px 16px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--surface-2);
  overflow-x: auto;
  font-size: 12.5px;
  line-height: 1.7;
}

.markdown-renderer pre code {
  padding: 0;
  background: none;
  color: var(--text-2);
  font-size: 12.5px;
}

.markdown-renderer .hljs {
  background: transparent;
}

.markdown-renderer .hljs-keyword,
.markdown-renderer .hljs-selector-tag,
.markdown-renderer .hljs-built_in,
.markdown-renderer .hljs-name,
.markdown-renderer .hljs-tag {
  color: #2f5f9e;
}

.markdown-renderer .hljs-string,
.markdown-renderer .hljs-title,
.markdown-renderer .hljs-section,
.markdown-renderer .hljs-attribute,
.markdown-renderer .hljs-literal,
.markdown-renderer .hljs-template-tag,
.markdown-renderer .hljs-template-variable,
.markdown-renderer .hljs-type,
.markdown-renderer .hljs-addition {
  color: #8a5a1a;
}

.markdown-renderer .hljs-comment,
.markdown-renderer .hljs-quote,
.markdown-renderer .hljs-deletion,
.markdown-renderer .hljs-meta {
  color: var(--text-3);
}

.markdown-renderer .hljs-number,
.markdown-renderer .hljs-regexp,
.markdown-renderer .hljs-selector-attr,
.markdown-renderer .hljs-selector-pseudo,
.markdown-renderer .hljs-variable {
  color: #6b4f9e;
}

.markdown-renderer .hljs-function {
  color: #2f5f9e;
}

.markdown-renderer .hljs-class .hljs-title {
  color: #3f7d5c;
}

/* 深色主题下语法高亮单独一套，避免深底上看不清 */
:root[data-theme="dark"] .markdown-renderer .hljs-keyword,
:root[data-theme="dark"] .markdown-renderer .hljs-built_in,
:root[data-theme="dark"] .markdown-renderer .hljs-name,
:root[data-theme="dark"] .markdown-renderer .hljs-tag,
:root[data-theme="dark"] .markdown-renderer .hljs-function {
  color: #9dbde8;
}

:root[data-theme="dark"] .markdown-renderer .hljs-string,
:root[data-theme="dark"] .markdown-renderer .hljs-title,
:root[data-theme="dark"] .markdown-renderer .hljs-attribute,
:root[data-theme="dark"] .markdown-renderer .hljs-section {
  color: #d8ae6a;
}

:root[data-theme="dark"] .markdown-renderer .hljs-number,
:root[data-theme="dark"] .markdown-renderer .hljs-variable,
:root[data-theme="dark"] .markdown-renderer .hljs-literal {
  color: #b9a6e0;
}

.markdown-renderer a {
  color: var(--accent);
  text-decoration: none;
  border-bottom: 1px solid transparent;
  transition: border-color var(--dur) var(--ease);
}

.markdown-renderer a:hover {
  border-bottom-color: var(--accent);
}

.markdown-renderer img.markdown-image {
  max-width: 100% !important;
  max-height: 340px;
  width: auto;
  height: auto;
  object-fit: contain;
  display: block;
  margin: 12px 0;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: #ffffff;
}

.markdown-renderer a.image-link {
  display: inline-block;
  margin: 8px 0;
  padding: 7px 12px;
  border: 1px solid var(--border);
  border-radius: 7px;
  color: var(--text-2);
  font-size: 12.5px;
  transition: border-color var(--dur) var(--ease);
}

.markdown-renderer a.image-link:hover {
  border-color: var(--border-strong);
}

/* 表格：细横线，不要竖线格子 */
.markdown-renderer table {
  width: 100%;
  margin: 14px 0;
  border-collapse: collapse;
  font-size: 12.5px;
}

.markdown-renderer th,
.markdown-renderer td {
  padding: 8px 10px;
  border-bottom: 1px solid var(--border);
  text-align: left;
  vertical-align: top;
}

.markdown-renderer th {
  font-weight: 500;
  color: var(--text-3);
  white-space: nowrap;
}

.markdown-renderer tr:last-child td {
  border-bottom: none;
}

.markdown-renderer blockquote {
  margin: 12px 0;
  padding: 2px 0 2px 14px;
  border-left: 2px solid var(--border-strong);
  color: var(--text-2);
}

.markdown-renderer blockquote p {
  margin: 3px 0;
}

.markdown-renderer hr {
  height: 1px;
  border: none;
  margin: 22px 0;
  background: var(--border);
}

.markdown-renderer input[type="checkbox"] {
  margin-right: 6px;
  vertical-align: middle;
  accent-color: var(--accent);
}

.markdown-renderer input[type="checkbox"]:checked + span {
  text-decoration: line-through;
  color: var(--text-3);
}
</style>
