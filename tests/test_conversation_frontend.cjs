// Exercise the actual Vue setup script with synthetic HTTP responses, no model calls.
const { test } = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const root = path.resolve(__dirname, '..')
const ts = require(path.join(root, 'frontend/node_modules/typescript'))
const vue = fs.readFileSync(path.join(root, 'frontend/src/App.vue'), 'utf8')
const setup = vue.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1].replace(/^import .*from 'vue'\r?$/m, '')
const compiled = ts.transpileModule(setup, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText

function harness(fetch, values = new Map()) {
  const module = { exports: {} }
  vm.runInNewContext(compiled + '\nmodule.exports={userId,tenantId,threadId,query,loading,historyBusy,historyError,conversations,conversationTotal,messages,restoreHistory,createNewChat,selectConversation,loadMoreHistory,runResearch};', {
    module, fetch, ref: value => ({ value }), nextTick: async () => {}, onMounted: () => {}, watch: () => {},
    localStorage: { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) },
    crypto: require('node:crypto').webcrypto, URLSearchParams, TextDecoder, AbortSignal, setTimeout, clearTimeout,
  })
  return module.exports
}
const response = body => new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } })
const session = id => ({ thread_id: id, title: id, created_at: '2026-10-20T00:00:00Z', updated_at: '2026-10-20T00:00:00Z', message_count: 2 })

test('refresh restores the saved conversation and full messages', async () => {
  const values = new Map([['zhiyan.active:["default_tenant","user01"]', 'older']])
  const app = harness(async url => url.includes('/conversations?')
    ? response({ conversations: [session('latest'), session('older')], total: 2 })
    : response({ conversation: session('older'), messages: [{ id: 'q', role: 'user', content: 'Saved question' }, { id: 'a', role: 'assistant', content: 'Saved answer' }] }), values)
  await app.restoreHistory()
  assert.equal(app.threadId.value, 'older')
  assert.equal(app.messages.value[1].content, 'Saved answer')
  assert.equal(app.historyBusy.value, false)
})

test('refreshing an untouched draft does not leave the composer blocked', async () => {
  const values = new Map([['zhiyan.active:["default_tenant","user01"]', 'unpersisted-draft']])
  const app = harness(async url => {
    assert.ok(url.includes('/conversations?'))
    return response({ conversations: [], total: 0 })
  }, values)
  await app.restoreHistory()
  assert.ok(app.threadId.value)
  assert.notEqual(app.threadId.value, 'unpersisted-draft')
  assert.equal(app.historyError.value, '')
  assert.equal(app.historyBusy.value, false)
})

test('new conversation changes ID and does not retain displayed messages', async () => {
  const app = harness(async (url, options) => options?.method === 'POST'
    ? response({ ...session('new-id'), message_count: 0 })
    : response({ conversations: [session('new-id')], total: 1 }))
  app.historyBusy.value = false
  app.threadId.value = 'old-id'
  app.messages.value = [{ role: 'assistant', content: 'Old answer' }]
  await app.createNewChat()
  assert.equal(app.threadId.value, 'new-id')
  assert.equal(app.messages.value.some(m => m.content === 'Old answer'), false)
})

test('post-send refresh prevents a concurrent load-more request from skipping a page', async () => {
  let resolveRefresh, notifyRefresh
  const refresh = new Promise(resolve => { resolveRefresh = resolve })
  const requested = new Promise(resolve => { notifyRefresh = resolve })
  let appendRequests = 0
  const app = harness(async url => {
    if (url.endsWith('/stream')) return new Response('data: {"type":"final","final":"Synthetic answer"}\n\n')
    if (new URL('http://local' + url).searchParams.has('offset')) {
      appendRequests++
      return response({ conversations: Array.from({ length: 20 }, (_, i) => session(String(100 + i))), total: 120 })
    }
    notifyRefresh()
    return refresh
  })
  app.historyBusy.value = false
  app.threadId.value = 'current'
  app.query.value = 'Synthetic question'
  app.conversations.value = Array.from({ length: 100 }, (_, i) => session(String(i)))
  app.conversationTotal.value = 120
  const running = app.runResearch()
  await requested
  try {
    await app.loadMoreHistory()
    assert.equal(appendRequests, 0, 'Load-more must wait until replacement refresh finishes')
  } finally {
    resolveRefresh(response({ conversations: Array.from({ length: 50 }, (_, i) => session(String(i))), total: 120 }))
    await running
  }
  assert.equal(app.conversations.value.length, 50)
  assert.equal(app.loading.value, false)
})
