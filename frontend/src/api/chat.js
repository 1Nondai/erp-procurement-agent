/**
 * 对话 API 模块
 *
 * 提供流式对话、中断恢复和会话状态查询接口
 */

import { identityHeaders } from './identity'

const API_BASE = '/api/chat'

/**
 * 流式对话
 *
 * 使用 fetch + ReadableStream 接收流式响应（支持 POST 请求）
 *
 * @param {string} message - 用户消息
 * @param {string|null} threadId - 会话 ID，为空则创建新会话
 * @param {Object} callbacks - 回调函数对象
 * @param {Function} callbacks.onToken - 接收到 token 时的回调 (content: string, source: string) => void
 * @param {Function} callbacks.onToolStart - 工具开始调用时的回调 (tool: object) => void
 * @param {Function} callbacks.onToolArgs - 工具参数片段的回调 (args: string, source: string) => void
 * @param {Function} callbacks.onToolResult - 工具结果返回时的回调 (tool: object) => void
 * @param {Function} callbacks.onToolEnd - 工具调用结束时的回调 (tool: object) => void
 * @param {Function} callbacks.onInterrupt - 检测到中断时的回调 (interruptData: object) => void
 * @param {Function} callbacks.onDone - 流结束时的回调 (data: object) => void
 * @param {Function} callbacks.onError - 发生错误时的回调 (error: Error) => void
 * @param {AbortSignal} signal - 可选的 AbortSignal，用于取消请求
 * @returns {Promise} 返回包含 thread_id, content, tool_calls 的结果
 */
export async function streamChat(message, threadId = null, callbacks = {}, signal = null) {
  // 用于收集完整内容
  let fullContent = ''
  let toolCalls = []
  // 工具调用栈，支持嵌套工具调用（主代理调 task → 子代理调工具）
  let toolStack = []

  try {
    // 使用 fetch 发送 POST 请求，支持 AbortController
    const response = await fetch(`${API_BASE}/stream`, {
      method: 'POST',
      // 身份随请求头传递：后端用它定位 /memories/{user_id}/ 并做会话归属校验
      headers: identityHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({
        message: message,
        thread_id: threadId
      }),
      signal: signal  // 支持取消请求
    })

    if (!response.ok) {
      throw new Error(`请求失败: ${response.status} ${response.statusText}`)
    }

    return await _processStream(response, threadId, callbacks, fullContent, toolCalls, toolStack)

  } catch (error) {
    if (error.name === 'AbortError') {
      // 用户主动取消，返回已收集的内容
      console.log('[ChatAPI] 请求已被用户取消')
      const result = {
        thread_id: threadId,
        content: fullContent,
        tool_calls: toolCalls,
        aborted: true
      }
      callbacks.onDone?.(result)
      return result
    }
    console.error('[ChatAPI] 流式请求失败:', error)
    callbacks.onError?.(error)
    throw error
  }
}

/**
 * 流式中断恢复
 *
 * 当中断发生后，前端收集用户决策/补充数据，通过此函数恢复 Agent 执行。
 *
 * @param {string} threadId - 会话 ID
 * @param {Object} resumeData - 恢复数据，格式取决于中断类型：
 *   - 数据补充: { supplement: "用户输入的补充信息" }
 *   - HITL 审批: { decisions: [{ type: "approve" }] } 或 [{ type: "reject" }]
 * @param {Object} callbacks - 回调函数对象（与 streamChat 相同）
 * @param {AbortSignal} signal - 可选的 AbortSignal，用于取消请求
 * @returns {Promise} 返回包含 thread_id, content, tool_calls 的结果
 */
export async function resumeChat(threadId, resumeData, callbacks = {}, signal = null) {
  let fullContent = ''
  let toolCalls = []
  let toolStack = []

  try {
    const response = await fetch(`${API_BASE}/${threadId}/resume`, {
      method: 'POST',
      headers: identityHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ resume: resumeData }),
      signal: signal
    })

    if (!response.ok) {
      throw new Error(`恢复请求失败: ${response.status} ${response.statusText}`)
    }

    return await _processStream(response, threadId, callbacks, fullContent, toolCalls, toolStack)

  } catch (error) {
    if (error.name === 'AbortError') {
      console.log('[ChatAPI] 恢复请求已被用户取消')
      const result = {
        thread_id: threadId,
        content: fullContent,
        tool_calls: toolCalls,
        aborted: true
      }
      callbacks.onDone?.(result)
      return result
    }
    console.error('[ChatAPI] 恢复请求失败:', error)
    callbacks.onError?.(error)
    throw error
  }
}

/**
 * 处理流式响应的内部函数（streamChat 和 resumeChat 共用）
 *
 * SSE 解析要点：
 * - 一个事件由若干行组成，以空行结束；可能有多行 data:，需要拼接后再 JSON.parse
 * - 流结束时缓冲区里可能还留着没带换行的最后一条事件，必须 flush
 * - 无论正常结束、中断返回还是抛错，都要在 finally 里释放 reader
 */
async function _processStream(response, threadId, callbacks, fullContent, toolCalls, toolStack) {
  // 获取 reader 来读取流
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  // 未解析完的残留文本
  let buffer = ''
  // 当前事件累积的 data 行
  let dataLines = []

  /**
   * 消费一行 SSE 文本；遇到空行（事件结束）时返回该事件的原始 data 文本，否则返回 null
   */
  function consumeLine(line) {
    // 去掉行尾 \r，兼容 CRLF
    const text = line.replace(/\r$/, '')

    // 空行 = 一个事件结束
    if (!text) {
      if (dataLines.length === 0) return null
      const raw = dataLines.join('\n')
      dataLines = []
      return raw
    }

    // 只关心 data 字段，event:/id:/retry: 和注释行一律忽略
    if (text.startsWith('data:')) {
      dataLines.push(text.slice(5).replace(/^ /, ''))
    }
    return null
  }

  /**
   * 解析并分发单个事件；返回结果对象表示流可以结束，返回 null 表示继续读
   */
  function dispatchEvent(raw) {
    if (!raw) return null

    let data
    try {
      data = JSON.parse(raw)
    } catch (parseError) {
      // 这里只吞 JSON 解析失败（服务端非 JSON 载荷），业务 error 不受影响
      console.warn('[ChatAPI] 解析 SSE 数据失败:', parseError)
      return null
    }

    switch (data.type) {
      case 'token':
        // AI 生成的文本片段
        fullContent += data.content
        callbacks.onToken?.(data.content, data.source)
        break

      case 'tool_start': {
        // 工具开始调用，压入栈
        const newTool = {
          id: data.tool_call_id,
          name: data.tool_name,
          args: '',
          source: data.source,
          text: '',
          images: []
        }
        toolStack.push(newTool)
        callbacks.onToolStart?.(newTool)
        break
      }

      case 'tool_args':
        // 工具参数片段，更新栈顶工具
        if (toolStack.length > 0) {
          toolStack[toolStack.length - 1].args += data.args
        }
        callbacks.onToolArgs?.(data.args, data.source)
        break

      case 'tool_result':
        // 工具执行结果，弹出栈顶工具并更新
        if (toolStack.length > 0) {
          const finished = toolStack.pop()
          finished.text = data.text || ''
          finished.images = data.images || []
          toolCalls.push({ ...finished })
          callbacks.onToolResult?.(finished)
        }
        break

      case 'tool_end':
        // 工具调用结束
        callbacks.onToolEnd?.({
          id: data.tool_call_id,
          name: data.tool_name,
          source: data.source
        })
        break

      case 'interrupt': {
        // ★ Human-in-the-Loop 中断检测
        console.log('[ChatAPI] 检测到中断:', data.interrupt_type)
        callbacks.onInterrupt?.(data)
        // 中断事件后流会结束，与 done 走同一个出口（带 interrupted 标记），
        // 否则调用方拿不到 thread_id，首轮中断后审批按钮会失效
        const interruptResult = {
          thread_id: data.thread_id || threadId,
          content: fullContent,
          tool_calls: toolCalls,
          interrupted: true,
          interrupt_data: data
        }
        callbacks.onDone?.(interruptResult)
        return interruptResult
      }

      case 'done': {
        // 流结束
        const result = {
          thread_id: data.thread_id,
          content: data.content || fullContent,
          tool_calls: toolCalls,
          interrupted: data.interrupted || false
        }
        callbacks.onDone?.(result)
        return result
      }

      case 'error': {
        // 服务端业务错误：打标记后上抛，交给 streamChat/resumeChat 的 catch 统一走 onError，
        // 不能在本层的解析 catch 里被当成"JSON 解析失败"吞掉
        const businessError = new Error(data.message || '服务端返回错误')
        businessError.isBusinessError = true
        businessError.code = data.code
        throw businessError
      }

      default:
        break
    }

    return null
  }

  try {
    while (true) {
      const { done, value } = await reader.read()

      if (done) {
        break
      }

      // 解码数据
      buffer += decoder.decode(value, { stream: true })

      // 按行切分，最后一段可能是不完整的行，留在 buffer 里
      const lines = buffer.split('\n')
      buffer = lines.pop()

      for (const line of lines) {
        const raw = consumeLine(line)
        if (raw === null) continue
        const finished = dispatchEvent(raw)
        if (finished) return finished
      }
    }

    // 流结束：先 flush 解码器残留，再处理缓冲区里没换行结尾的最后一条
    buffer += decoder.decode()
    for (const line of buffer.split('\n')) {
      const raw = consumeLine(line)
      if (raw === null) continue
      const finished = dispatchEvent(raw)
      if (finished) return finished
    }

    // 最后一条事件没有空行收尾，仍留在 dataLines 里，补一次分发
    if (dataLines.length > 0) {
      const finished = dispatchEvent(dataLines.join('\n'))
      if (finished) return finished
    }

    // 如果没有收到 done 事件，返回已收集的内容
    return {
      thread_id: threadId,
      content: fullContent,
      tool_calls: toolCalls
    }
  } finally {
    // 中断/完成提前 return 时流还没读完，必须 cancel 掉，否则连接挂住
    try {
      await reader.cancel()
    } catch (e) {
      /* 流已关闭，忽略 */
    }
    try {
      reader.releaseLock()
    } catch (e) {
      /* 已释放，忽略 */
    }
  }
}
