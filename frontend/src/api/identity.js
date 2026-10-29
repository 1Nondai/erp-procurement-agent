/**
 * 请求身份模块
 *
 * 与后端 api_view/identity.py 对应：所有请求统一携带 X-User-Id / X-Username，
 * 后端据此决定会话归属与记忆命名空间（/memories/{user_id}/）。
 *
 * 为什么放在前端而不是写死在后端：
 *   身份必须由请求携带。当前还没有接入登录体系，先用本地存储承载"当前用户"；
 *   接入 JWT/OAuth 后只需把 getUserId() 改成从 token 解析，其余调用点不用动。
 *
 * 如何切换用户（临时手段，P2 会补界面入口）：
 *   浏览器控制台执行 setUser('zhangsan', '张三') 后刷新页面即可。
 */

const USER_ID_KEY = 'erp_user_id'
const USERNAME_KEY = 'erp_username'

// 与后端 config.DEFAULT_USER_ID 保持一致：未设置时的兜底身份
const DEV_DEFAULT_USER_ID = 'dev_default'

export function getUserId() {
  return localStorage.getItem(USER_ID_KEY) || DEV_DEFAULT_USER_ID
}

export function getUsername() {
  return localStorage.getItem(USERNAME_KEY) || getUserId()
}

export function setUser(userId, username = '') {
  if (!userId) return
  localStorage.setItem(USER_ID_KEY, userId)
  localStorage.setItem(USERNAME_KEY, username || userId)
}

/**
 * 生成带身份头的 headers
 *
 * @param {Object} extra - 额外 header（如 Content-Type）
 * @returns {Object} 合并后的 headers
 */
export function identityHeaders(extra = {}) {
  return {
    'X-User-Id': getUserId(),
    'X-Username': getUsername(),
    ...extra,
  }
}
