<script setup lang="ts">
import { nextTick, onMounted, ref, watch } from 'vue'

type StreamEvent = {
  type: 'status' | 'phase' | 'route' | 'final' | 'error'
  message?: string
  final?: string
  node?: string
}

type ChatMessage = {
  id: string
  role: 'user' | 'assistant' | 'status'
  content: string
}

type Conversation = {
  thread_id: string
  title: string
  created_at: string
  updated_at: string
  message_count: number
}
type ConversationDetail = { conversation: Conversation; messages: ChatMessage[] }
const readStorage = (key: string): string | null => {
  try { return localStorage.getItem(key) } catch { return null }
}
const writeStorage = (key: string, value: string) => {
  try { localStorage.setItem(key, value) } catch { /* History remains available on the server. */ }
}
const savedIdentity = (() => {
  try {
    const value = JSON.parse(readStorage('zhiyan.identity') || '{}')
    return value && typeof value === 'object' ? value : {}
  } catch { return {} }
})()
const userId = ref(typeof savedIdentity.user_id === 'string' ? savedIdentity.user_id : 'user01')
const threadId = ref('')
const tenantId = ref(typeof savedIdentity.tenant_id === 'string' ? savedIdentity.tenant_id : 'default_tenant')
const conversations = ref<Conversation[]>([])
const conversationTotal = ref(0)
const historyBusy = ref(true)
const historyError = ref('')
let historyVersion = 0
const identity = () => ({ tenant_id: tenantId.value.trim() || 'default_tenant', user_id: userId.value.trim() || 'default_user' })
const activeKey = (scope = identity()) => `zhiyan.active:${JSON.stringify([scope.tenant_id, scope.user_id])}`
const scopeQuery = (scope = identity()) => new URLSearchParams(scope)
const conversationDate = (value: string) => new Date(value).toLocaleString('zh-CN', {
  timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
})
const query = ref('')
const loading = ref(false)
const errorMessage = ref('')
const messageListRef = ref<HTMLElement | null>(null)
const composerRef = ref<HTMLTextAreaElement | null>(null)
const progressLogs = ref<string[]>([])
const starterPrompts = [
  {
    title: '深度调研',
    prompt:
      '请调研“企业知识库 Agent 平台”市场，按市场规模、主要竞品、收费模式三部分输出，并在每部分附上可追溯来源链接。',
  },
  {
    title: '方案对比',
    prompt:
      '我们要做多 Agent 研究助手，请对比“纯大模型直答”“RAG 单 Agent”“多 Agent 协作”三种方案，给出优缺点、适用场景与推荐结论。',
  },
  {
    title: '知识问答',
    prompt: '请解释这个项目里“意图分流”的作用，以及简单问题和复杂问题分别会走哪条链路。',
  },
  {
    title: '落地计划',
    prompt: '请把“上线一个可用的zhiyan研究助手”拆成两周计划，按每天输出任务、验收标准和风险点。',
  },
]
const messages = ref<ChatMessage[]>([
  {
    id: `m-${Date.now()}`,
    role: 'assistant',
    content: '你好，我是zhiyan。你可以直接提问，我会根据意图自动走快速回答或完整研究链路。',
  },
])

const escapeHtml = (value: string): string =>
  value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;')

const markdownToHtml = (markdown: string): string => {
  const codeBlocks: string[] = []
  let text = markdown.replace(/```([\s\S]*?)```/g, (_, block) => {
    const index = codeBlocks.length
    codeBlocks.push(`<pre><code>${escapeHtml(String(block).trim())}</code></pre>`)
    return `@@CODE_BLOCK_${index}@@`
  })
  const lines = text.split('\n')
  const out: string[] = []
  let inList = false
  const closeList = () => {
    if (inList) {
      out.push('</ul>')
      inList = false
    }
  }
  for (const rawLine of lines) {
    const line = rawLine.trim()
    if (!line) {
      closeList()
      continue
    }
    if (line.startsWith('# ')) {
      closeList()
      out.push(`<h1>${escapeHtml(line.slice(2))}</h1>`)
      continue
    }
    if (line.startsWith('## ')) {
      closeList()
      out.push(`<h2>${escapeHtml(line.slice(3))}</h2>`)
      continue
    }
    if (line.startsWith('### ')) {
      closeList()
      out.push(`<h3>${escapeHtml(line.slice(4))}</h3>`)
      continue
    }
    if (line.startsWith('- ') || line.startsWith('* ')) {
      if (!inList) {
        out.push('<ul>')
        inList = true
      }
      out.push(`<li>${escapeHtml(line.slice(2))}</li>`)
      continue
    }
    closeList()
    out.push(`<p>${escapeHtml(line)}</p>`)
  }
  closeList()
  let html = out.join('')
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
  html = html.replace(/\*(.+?)\*/g, '<em>$1</em>')
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>')
  html = html.replace(/\[([^[\]]+)\]\((https?:\/\/[^)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>')
  html = html.replace(/@@CODE_BLOCK_(\d+)@@/g, (_, idx) => codeBlocks[Number(idx)] || '')
  return html
}

const renderMessageHtml = (message: ChatMessage) => markdownToHtml(message.content || '')

const scrollToBottom = async () => {
  await nextTick()
  const el = messageListRef.value
  if (el) {
    el.scrollTop = el.scrollHeight
  }
}

const resetChatView = () => {
  messages.value = [
    {
      id: `m-${Date.now()}`,
      role: 'assistant',
      content: '已开始新会话。你可以继续提问。',
    },
  ]
  progressLogs.value = []
  errorMessage.value = ''
  query.value = ''
}

const historyRequest = async <T,>(url: string, options?: RequestInit): Promise<T> => {
  const response = await fetch(url, { ...options, signal: options?.signal || AbortSignal.timeout(15000) })
  if (!response.ok) throw new Error(response.status === 404 ? '会话不存在，请刷新历史列表。' : '会话记录暂时无法读取，请重试。')
  return response.json() as Promise<T>
}

const activateConversation = async (detail: ConversationDetail) => {
  threadId.value = detail.conversation.thread_id
  resetChatView()
  if (detail.messages.length) messages.value = detail.messages
  writeStorage(activeKey(), threadId.value)
  await scrollToBottom()
}

const restoreHistory = async () => {
  const version = ++historyVersion
  const scope = identity()
  historyBusy.value = true
  historyError.value = ''
  threadId.value = ''
  conversations.value = []
  conversationTotal.value = 0
  resetChatView()
  try {
    const page = await historyRequest<{ conversations: Conversation[]; total: number }>(`/api/v1/conversations?${scopeQuery(scope)}`)
    if (version !== historyVersion) return
    conversations.value = page.conversations
    conversationTotal.value = page.total
    const previous = readStorage(activeKey(scope))
    const selected = (page.total > 0 ? previous : null) || page.conversations[0]?.thread_id
    if (selected) {
      let detail: ConversationDetail
      try {
        detail = await historyRequest<ConversationDetail>(`/api/v1/conversations/${encodeURIComponent(selected)}?${scopeQuery(scope)}`)
      } catch (error) {
        if (version !== historyVersion) return
        const fallback = page.conversations[0]
        if (!fallback || fallback.thread_id === selected) throw error
        detail = await historyRequest<ConversationDetail>(`/api/v1/conversations/${encodeURIComponent(fallback.thread_id)}?${scopeQuery(scope)}`)
      }
      if (version !== historyVersion) return
      await activateConversation(detail)
    } else {
      // An untouched draft is only created on the server when sent or explicitly saved by New Chat.
      threadId.value = crypto.randomUUID()
      writeStorage(activeKey(scope), threadId.value)
    }
  } catch (error) {
    if (version === historyVersion) historyError.value = error instanceof Error ? error.message : '读取会话失败'
  } finally {
    if (version === historyVersion) historyBusy.value = false
  }
}

const selectConversation = async (selected: string) => {
  if (!selected || loading.value || historyBusy.value || selected === threadId.value) return
  const version = ++historyVersion
  historyBusy.value = true
  historyError.value = ''
  try {
    const detail = await historyRequest<ConversationDetail>(`/api/v1/conversations/${encodeURIComponent(selected)}?${scopeQuery()}`)
    if (version === historyVersion) await activateConversation(detail)
  } catch (error) {
    if (version === historyVersion) historyError.value = error instanceof Error ? error.message : '读取会话失败'
  } finally {
    if (version === historyVersion) historyBusy.value = false
  }
}

const refreshHistoryList = async (append = false) => {
  const version = historyVersion
  const params = scopeQuery()
  if (append) params.set('offset', String(conversations.value.length))
  const page = await historyRequest<{ conversations: Conversation[]; total: number }>(`/api/v1/conversations?${params}`)
  if (version !== historyVersion) return
  conversations.value = append ? [...conversations.value, ...page.conversations] : page.conversations
  conversationTotal.value = page.total
}

const loadMoreHistory = async () => {
  if (loading.value || historyBusy.value) return
  const version = historyVersion
  historyBusy.value = true
  try { await refreshHistoryList(true) }
  catch { if (version === historyVersion) historyError.value = '加载更多会话失败，请重试。' }
  finally { if (version === historyVersion) historyBusy.value = false }
}

const createNewChat = async () => {
  if (loading.value || historyBusy.value) return
  const version = ++historyVersion
  historyBusy.value = true
  historyError.value = ''
  try {
    const conversation = await historyRequest<Conversation>('/api/v1/conversations', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(identity()),
    })
    if (version !== historyVersion) return
    await activateConversation({ conversation, messages: [] })
    await refreshHistoryList()
  } catch (error) {
    if (version === historyVersion) historyError.value = error instanceof Error ? error.message : '新建会话失败'
  } finally {
    if (version === historyVersion) historyBusy.value = false
  }
}

const usePrompt = async (prompt: string) => {
  query.value = prompt
  errorMessage.value = ''
  await nextTick()
  composerRef.value?.focus()
}

const pushProgress = (message: string) => {
  const msg = message.trim()
  if (!msg) return
  const last = progressLogs.value[progressLogs.value.length - 1]
  if (last === msg) return
  progressLogs.value.push(msg)
  if (progressLogs.value.length > 6) {
    progressLogs.value = progressLogs.value.slice(-6)
  }
}

const runResearch = async () => {
  const userText = query.value.trim()
  if (!userText || loading.value || historyBusy.value || !threadId.value) return
  loading.value = true
  errorMessage.value = ''
  progressLogs.value = []
  query.value = ''
  messages.value.push({ id: `u-${Date.now()}`, role: 'user', content: userText })
  const statusId = `s-${Date.now()}`
  messages.value.push({ id: statusId, role: 'status', content: '正在初始化执行链路...' })
  const renderStatusText = () => {
    const statusMessage = messages.value.find((item) => item.id === statusId)
    if (!statusMessage) return
    const latest = progressLogs.value.slice(-8)
    statusMessage.content = ['正在处理中...', ...latest].map((line) => `- ${line}`).join('\n')
  }
  renderStatusText()
  await scrollToBottom()
  try {
    const response = await fetch('/api/v1/research/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        query: userText,
        user_id: userId.value.trim() || 'default_user',
        thread_id: threadId.value.trim() || 'default_thread',
        tenant_id: tenantId.value.trim() || 'default_tenant',
      }),
    })
    if (!response.ok) {
      const text = await response.text()
      throw new Error(text || `请求失败: ${response.status}`)
    }
    if (!response.body) {
      throw new Error('流式响应不可用')
    }
    const reader = response.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buffer = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const parts = buffer.split('\n\n')
      buffer = parts.pop() || ''
      for (const part of parts) {
        if (!part.startsWith('data: ')) continue
        const jsonText = part.slice(6).trim()
        if (!jsonText) continue
        const event = JSON.parse(jsonText) as StreamEvent
        if (event.type === 'status' || event.type === 'phase' || event.type === 'route') {
          const prefix = event.type === 'phase' && event.node ? `[${event.node}] ` : ''
          pushProgress(`${prefix}${event.message || ''}`)
          renderStatusText()
        }
        if (event.type === 'final') {
          messages.value = messages.value.filter((item) => item.id !== statusId)
          messages.value.push({
            id: `a-${Date.now()}`,
            role: 'assistant',
            content: event.final || '已完成，但未返回正文。',
          })
        }
        if (event.type === 'error') {
          throw new Error(event.message || '服务端执行异常')
        }
      }
      await scrollToBottom()
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '请求失败'
    messages.value = messages.value.filter((item) => item.id !== statusId)
    messages.value.push({
      id: `e-${Date.now()}`,
      role: 'assistant',
      content: `请求失败：${errorMessage.value}`,
    })
  } finally {
    await scrollToBottom()
    historyBusy.value = true
    try { await refreshHistoryList() }
    catch { historyError.value = '历史列表更新失败，刷新页面可重新读取。' }
    finally {
      historyBusy.value = false
      loading.value = false
    }
  }
}

onMounted(restoreHistory)
let identityTimer: ReturnType<typeof setTimeout>
watch([userId, tenantId], () => {
  ++historyVersion
  threadId.value = ''
  historyBusy.value = true
  clearTimeout(identityTimer)
  writeStorage('zhiyan.identity', JSON.stringify(identity()))
  identityTimer = setTimeout(restoreHistory, 350)
})
</script>

<template>
  <div class="chat-shell">
    <aside class="chat-sidebar" aria-label="会话导航">
      <h1 class="sidebar-brand">zhiyan</h1>
      <button class="new-chat-btn" :disabled="loading || historyBusy" @click="createNewChat">新建会话</button>
      <section class="conversation-history" aria-label="历史会话">
        <div class="history-heading">
          <h2>历史会话</h2>
          <span>{{ conversationTotal }}</span>
        </div>
        <p v-if="historyBusy" class="history-status" role="status">正在读取会话…</p>
        <p v-if="historyError" class="history-error" role="alert">{{ historyError }}</p>
        <button v-if="historyError" class="history-more" :disabled="loading || historyBusy" @click="restoreHistory">重新加载</button>
        <nav class="history-list" aria-label="会话列表">
          <button v-for="item in conversations" :key="item.thread_id" class="history-item"
            :class="{ active: item.thread_id === threadId }" :aria-current="item.thread_id === threadId ? 'page' : undefined"
            :title="item.title" :disabled="loading || historyBusy" @click="selectConversation(item.thread_id)">
            <span class="history-title">{{ item.title }}</span>
            <span class="history-meta">{{ conversationDate(item.updated_at) }} · {{ item.message_count }} 条消息</span>
          </button>
        </nav>
        <p v-if="!historyBusy && !historyError && !conversations.length" class="history-status">还没有历史会话。</p>
        <button v-if="conversations.length < conversationTotal" class="history-more" :disabled="loading || historyBusy" @click="loadMoreHistory">加载更多</button>
      </section>
    </aside>

    <main class="chat-main">
      <header class="main-header">
        <h2 :title="conversations.find(item => item.thread_id === threadId)?.title">
          {{ conversations.find(item => item.thread_id === threadId)?.title || '研究工作台' }}
        </h2>
        <details class="session-settings">
          <summary>会话设置</summary>
          <div class="settings-panel">
            <h3>会话设置</h3>
            <div class="settings-group">
              <label for="user-id">用户 ID</label>
              <input id="user-id" v-model="userId" class="sidebar-input" :disabled="loading" autocomplete="off" />
            </div>
            <div class="settings-group">
              <label for="tenant-id">租户 ID</label>
              <input id="tenant-id" v-model="tenantId" class="sidebar-input" :disabled="loading" autocomplete="off" />
            </div>
            <div class="settings-group">
              <label for="thread-id">会话 ID</label>
              <input id="thread-id" :value="threadId" class="sidebar-input" readonly title="新建时自动生成，选择历史会话可切换" />
            </div>
          </div>
        </details>
      </header>
      <div class="mobile-conversations">
        <select aria-label="切换历史会话" :value="threadId" :disabled="loading || historyBusy"
          @change="selectConversation(($event.target as HTMLSelectElement).value)">
          <option v-if="!conversations.some(item => item.thread_id === threadId)" :value="threadId">当前会话</option>
          <option v-for="item in conversations" :key="item.thread_id" :value="item.thread_id">{{ item.title }}</option>
        </select>
        <button :disabled="loading || historyBusy" @click="createNewChat">新建会话</button>
        <button v-if="conversations.length < conversationTotal" :disabled="loading || historyBusy" @click="loadMoreHistory">更多历史</button>
        <div v-if="historyError" class="mobile-history-error">
          <p role="alert">{{ historyError }}</p>
          <button :disabled="loading || historyBusy" @click="restoreHistory">重新加载</button>
        </div>
      </div>
      <div ref="messageListRef" class="message-list" :class="{ 'is-empty': messages.length === 1 && messages[0]?.id.startsWith('m-') }">
        <section v-if="messages.length === 1 && messages[0]?.id.startsWith('m-')" class="onboarding-panel" aria-label="开始研究">
          <h3>从一个问题开始</h3>
          <p class="onboarding-intro">说明研究目标、背景和期望的输出，或从下面的问题开始。</p>
          <div class="prompt-list">
            <button v-for="item in starterPrompts" :key="item.prompt" class="prompt-chip" @click="usePrompt(item.prompt)">
              <span class="prompt-title">{{ item.title }}</span>
              <span class="prompt-preview">{{ item.prompt }}</span>
            </button>
          </div>
        </section>
        <template v-else>
          <article v-for="message in messages" :key="message.id" class="message-row" :class="`role-${message.role}`">
            <p class="message-author">{{ message.role === 'user' ? '你' : message.role === 'status' ? '研究进度' : 'zhiyan' }}</p>
            <div class="bubble markdown-body" :role="message.role === 'status' ? 'status' : undefined" v-html="renderMessageHtml(message)"></div>
          </article>
        </template>
      </div>
      <div class="composer-area">
        <div class="composer">
          <textarea
            v-model="query"
            ref="composerRef"
            class="composer-input"
            :disabled="loading || historyBusy || !threadId"
            aria-label="研究问题"
            placeholder="输入你的问题，回车发送（Shift + Enter 换行）"
            @keydown.enter.exact.prevent="runResearch"
          />
          <button class="send-btn" :disabled="loading || historyBusy || !threadId || !query.trim()" @click="runResearch">
            {{ loading ? '研究中…' : '发送' }}
          </button>
        </div>
        <p v-if="errorMessage" class="error" role="alert">{{ errorMessage }}</p>
      </div>
    </main>
  </div>
</template>
