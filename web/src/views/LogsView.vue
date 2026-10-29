<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { accountsApi } from '@/api/accounts.ts'
import { systemApi } from '@/api/system.ts'
import { useToastStore } from '@/stores/toast.ts'
import type { CookiePushChange, CookiePushEvent, DumpFileInfo, LogEntry } from '@/types'
import Button from '@/components/ui/Button.vue'
import Modal from '@/components/ui/Modal.vue'
import {
  ArrowRight,
  ChevronDown,
  ChevronUp,
  CirclePause,
  CirclePlay,
  Cookie,
  FileJson,
  RefreshCw,
  Terminal,
  Trash2,
} from 'lucide-vue-next'

type TabKey = 'logs' | 'dumps' | 'pushes'

const toast = useToastStore()
const activeTab = ref<TabKey>('logs')
const logs = ref<LogEntry[]>([])
const lastSeq = ref(0)
const autoRefresh = ref(true)
const stickToBottom = ref(true)
const logContainer = ref<HTMLElement | null>(null)
const logFetching = ref(false)

const dumpFiles = ref<DumpFileInfo[]>([])
const dumpDir = ref('')
const dumpLoading = ref(false)
const dumpModalOpen = ref(false)
const selectedDumpName = ref('')
const selectedDumpContent = ref('')
const clearingLogs = ref(false)

const pushEntries = ref<CookiePushEvent[]>([])
const pushTotal = ref(0)
const pushCapacity = ref(200)
const pushFetching = ref(false)
const clearingPushes = ref(false)
const onlyPushChanges = ref(false)
const expandedPushes = ref<Set<number>>(new Set())

const deleteModalOpen = ref(false)
const deleteAll = ref(false)
const deleteTarget = ref<DumpFileInfo | null>(null)
const deleting = ref(false)

let pollTimer: ReturnType<typeof setInterval> | undefined

const levelColors: Record<string, string> = {
  DEBUG: 'text-slate-600 bg-slate-100',
  INFO: 'text-emerald-700 bg-emerald-50',
  WARNING: 'text-amber-700 bg-amber-50',
  ERROR: 'text-rose-700 bg-rose-50',
  CRITICAL: 'text-rose-700 bg-rose-100',
}

function levelClass(level: string): string {
  return levelColors[level.toUpperCase()] || 'text-gray-600 bg-gray-100'
}

function shortLogger(name: string): string {
  return name.replace(/^aistudio\./, '')
}

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${(size / (1024 * 1024)).toFixed(2)} MB`
}

function scrollToBottom() {
  const el = logContainer.value
  if (el) el.scrollTop = el.scrollHeight
}

function handleScroll() {
  const el = logContainer.value
  if (!el) return
  stickToBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < 48
}

async function loadLogs(reset = false) {
  if (logFetching.value) return
  logFetching.value = true
  try {
    const res = await systemApi.getLogs(reset ? 0 : lastSeq.value, 300)
    if (reset) {
      logs.value = res.entries
      lastSeq.value = res.last_seq
      stickToBottom.value = true
      await nextTick()
      scrollToBottom()
    } else if (res.entries.length > 0) {
      logs.value = logs.value.concat(res.entries).slice(-1000)
      lastSeq.value = res.last_seq
      if (stickToBottom.value) {
        await nextTick()
        scrollToBottom()
      }
    }
  } catch (err: unknown) {
    toast.error(err instanceof Error ? err.message : '获取运行日志失败')
  } finally {
    logFetching.value = false
  }
}

async function loadDumps() {
  dumpLoading.value = true
  try {
    const res = await systemApi.listDumpFiles(50)
    dumpFiles.value = res.files
    dumpDir.value = res.dir
  } catch (err: unknown) {
    toast.error(err instanceof Error ? err.message : '获取转储文件列表失败')
  } finally {
    dumpLoading.value = false
  }
}

async function loadPushHistory(silent = false) {
  if (pushFetching.value) return
  pushFetching.value = true
  try {
    const res = await accountsApi.getPushHistory(100)
    pushEntries.value = res.entries
    pushTotal.value = res.total
    pushCapacity.value = res.capacity
  } catch (err: unknown) {
    if (!silent) {
      toast.error(err instanceof Error ? err.message : '获取 Cookie 推送记录失败')
    }
  } finally {
    pushFetching.value = false
  }
}

const filteredPushEntries = computed(() =>
  onlyPushChanges.value
    ? pushEntries.value.filter(
        (entry) =>
          entry.first ||
          entry.changed ||
          entry.merged_changed ||
          entry.session_valid === false,
      )
    : pushEntries.value,
)

function togglePush(seq: number) {
  const next = new Set(expandedPushes.value)
  if (next.has(seq)) {
    next.delete(seq)
  } else {
    next.add(seq)
  }
  expandedPushes.value = next
}

function formatPushTime(at: number): string {
  return new Date(at * 1000).toLocaleString('zh-CN', { hour12: false })
}

function sessionLabel(value: boolean | null): string {
  if (value === true) return '登录态有效'
  if (value === false) return '登录态失效'
  return '登录态未验证'
}

function sessionBadgeClass(value: boolean | null): string {
  if (value === true) return 'bg-emerald-50 text-emerald-700'
  if (value === false) return 'bg-rose-50 text-rose-700'
  return 'bg-gray-100 text-gray-500'
}

function changeLabel(entry: CookiePushEvent): string {
  if (entry.first) return '首次推送'
  const core = entry.changes.filter((change) => change.category === 'core').length
  const rolling = entry.changes.filter(
    (change) => change.category === 'rolling',
  ).length
  if (core > 0) return `核心 Cookie 变化 ${core}`
  if (rolling > 0) return `滚动票变化 ${rolling}`
  if (entry.changes.length > 0) return `Cookie 变化 ${entry.changes.length}`
  return '与上次相同'
}

function changeBadgeClass(entry: CookiePushEvent): string {
  if (entry.first) return 'bg-slate-100 text-slate-600'
  if (entry.changes.some((change) => change.category === 'core')) {
    return 'bg-rose-50 text-rose-700'
  }
  if (entry.changes.length > 0) return 'bg-amber-50 text-amber-700'
  return 'bg-emerald-50 text-emerald-700'
}

interface ChangeGroup {
  key: string
  label: string
  titleClass: string
  items: CookiePushChange[]
}

function changeGroups(entry: CookiePushEvent): ChangeGroup[] {
  const groups: ChangeGroup[] = [
    {
      key: 'core',
      label: '核心身份 Cookie',
      titleClass: 'text-rose-700',
      items: entry.changes.filter((change) => change.category === 'core'),
    },
    {
      key: 'rolling',
      label: '滚动票',
      titleClass: 'text-amber-700',
      items: entry.changes.filter((change) => change.category === 'rolling'),
    },
    {
      key: 'other',
      label: '其他 Cookie',
      titleClass: 'text-gray-600',
      items: entry.changes.filter((change) => change.category === 'other'),
    },
  ]
  return groups.filter((group) => group.items.length > 0)
}

function kindLabel(kind: CookiePushChange['kind']): string {
  if (kind === 'added') return '新增'
  if (kind === 'removed') return '消失'
  return '变化'
}

function kindClass(kind: CookiePushChange['kind']): string {
  if (kind === 'added') return 'bg-emerald-50 text-emerald-700'
  if (kind === 'removed') return 'bg-rose-50 text-rose-700'
  return 'bg-amber-50 text-amber-700'
}

function cookieLocation(change: CookiePushChange): string {
  const domain = change.domain || '.google.com'
  return change.path && change.path !== '/' ? `${domain}${change.path}` : domain
}

async function clearPushHistory() {
  if (clearingPushes.value) return
  if (
    !window.confirm(
      '清空 Cookie 推送记录？只清空这个列表，不影响账号 Cookie。',
    )
  ) {
    return
  }
  clearingPushes.value = true
  try {
    const res = await accountsApi.clearPushHistory()
    pushEntries.value = []
    pushTotal.value = 0
    expandedPushes.value = new Set()
    toast.success(`已清空 ${res.cleared} 条推送记录`)
  } catch (err: unknown) {
    toast.error(err instanceof Error ? err.message : '清空推送记录失败')
  } finally {
    clearingPushes.value = false
  }
}

function switchTab(tab: TabKey) {
  activeTab.value = tab
  if (tab === 'dumps') {
    void loadDumps()
  } else if (tab === 'pushes') {
    void loadPushHistory()
  } else {
    void loadLogs()
  }
}

function toggleAutoRefresh() {
  autoRefresh.value = !autoRefresh.value
  if (autoRefresh.value) {
    void loadLogs()
  }
}

async function clearServerLogs() {
  if (clearingLogs.value) return
  clearingLogs.value = true
  try {
    const res = await systemApi.clearLogs()
    logs.value = []
    toast.success(`已清空 ${res.cleared} 条服务端日志`)
  } catch (err: unknown) {
    toast.error(err instanceof Error ? err.message : '清空日志失败')
  } finally {
    clearingLogs.value = false
  }
}

async function openDump(file: DumpFileInfo) {
  try {
    const data = await systemApi.getDumpFile(file.name)
    selectedDumpName.value = file.name
    selectedDumpContent.value = JSON.stringify(data, null, 2)
    dumpModalOpen.value = true
  } catch (err: unknown) {
    toast.error(err instanceof Error ? err.message : '读取转储文件失败')
  }
}

function askDeleteDump(file: DumpFileInfo) {
  deleteTarget.value = file
  deleteAll.value = false
  deleteModalOpen.value = true
}

function askClearDumps() {
  deleteTarget.value = null
  deleteAll.value = true
  deleteModalOpen.value = true
}

async function confirmDelete() {
  if (deleting.value) return
  deleting.value = true
  try {
    if (deleteAll.value) {
      const res = await systemApi.clearDumpFiles()
      toast.success(`已删除 ${res.deleted} 个转储文件`)
    } else if (deleteTarget.value) {
      await systemApi.deleteDumpFile(deleteTarget.value.name)
      toast.success('转储文件已删除')
    }
    deleteModalOpen.value = false
    await loadDumps()
  } catch (err: unknown) {
    toast.error(err instanceof Error ? err.message : '删除转储文件失败')
  } finally {
    deleting.value = false
  }
}

onMounted(async () => {
  await Promise.all([loadLogs(true), loadDumps()])
  pollTimer = setInterval(() => {
    if (document.visibilityState === 'hidden') return
    if (autoRefresh.value && activeTab.value === 'logs') {
      void loadLogs()
    }
    if (activeTab.value === 'pushes') {
      void loadPushHistory(true)
    }
  }, 2000)
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
})
</script>

<template>
  <div class="space-y-6">
    <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
      <div>
        <h2 class="text-xl font-bold text-gray-900 tracking-tight">
          运行日志
        </h2>
        <p class="mt-1 text-xs text-gray-500">
          在网页查看服务端最近日志、请求转储，以及每次浏览器 Cookie 推送与上一次的差异。
        </p>
      </div>

      <div class="inline-flex rounded-xl border border-gray-200 bg-white p-1 shadow-xs">
        <button
          type="button"
          class="px-3 py-1.5 rounded-lg text-xs font-medium transition-colors cursor-pointer"
          :class="activeTab === 'logs' ? 'bg-brand-50 text-brand-700' : 'text-gray-500 hover:text-gray-800'"
          @click="switchTab('logs')"
        >
          运行日志
        </button>
        <button
          type="button"
          class="px-3 py-1.5 rounded-lg text-xs font-medium transition-colors cursor-pointer"
          :class="activeTab === 'pushes' ? 'bg-brand-50 text-brand-700' : 'text-gray-500 hover:text-gray-800'"
          @click="switchTab('pushes')"
        >
          Cookie 推送
        </button>
        <button
          type="button"
          class="px-3 py-1.5 rounded-lg text-xs font-medium transition-colors cursor-pointer"
          :class="activeTab === 'dumps' ? 'bg-brand-50 text-brand-700' : 'text-gray-500 hover:text-gray-800'"
          @click="switchTab('dumps')"
        >
          请求转储
        </button>
      </div>
    </div>

    <div
      v-if="activeTab === 'logs'"
      class="bg-white border border-gray-200/80 rounded-2xl shadow-xs overflow-hidden"
    >
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-gray-100 px-5 py-3">
        <div class="flex items-center gap-2 text-sm font-semibold text-gray-900">
          <Terminal class="w-4 h-4 text-brand-600" />
          <span>实时日志</span>
          <span class="rounded-full bg-gray-100 px-2 py-0.5 text-[10px] font-medium text-gray-500">
            {{ logs.length }} 条
          </span>
          <span
            v-if="autoRefresh"
            class="text-[10px] font-normal text-emerald-600"
          >
            每 2 秒刷新
          </span>
        </div>

        <div class="flex items-center gap-2">
          <Button
            size="sm"
            variant="secondary"
            @click="toggleAutoRefresh"
          >
            <component
              :is="autoRefresh ? CirclePause : CirclePlay"
              class="w-3.5 h-3.5"
            />
            {{ autoRefresh ? '暂停' : '继续' }}
          </Button>
          <Button
            size="sm"
            variant="secondary"
            :loading="logFetching"
            @click="loadLogs(true)"
          >
            <RefreshCw class="w-3.5 h-3.5" />
            刷新
          </Button>
          <Button
            size="sm"
            variant="secondary"
            :loading="clearingLogs"
            title="清空服务端内存日志（不影响 docker logs）"
            @click="clearServerLogs"
          >
            <Trash2 class="w-3.5 h-3.5" />
            清空日志
          </Button>
        </div>
      </div>

      <div
        ref="logContainer"
        class="h-[62vh] overflow-y-auto bg-slate-50/60 font-mono text-[11px] leading-relaxed"
        @scroll="handleScroll"
      >
        <div
          v-if="logs.length === 0"
          class="flex h-full flex-col items-center justify-center gap-2 text-xs text-gray-400"
        >
          <Terminal class="w-8 h-8 text-gray-300" />
          <p>
            暂无日志。服务产生的日志会自动出现在这里。
          </p>
        </div>

        <div
          v-for="entry in logs"
          :key="entry.seq"
          class="border-b border-gray-100 px-4 py-1.5 last:border-0 hover:bg-white/80"
        >
          <div class="flex items-start gap-2">
            <span class="shrink-0 text-gray-400">{{ entry.time }}</span>
            <span
              class="shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold"
              :class="levelClass(entry.level)"
            >
              {{ entry.level }}
            </span>
            <span
              class="shrink-0 max-w-[9rem] truncate text-gray-400"
              :title="entry.logger"
            >
              {{ shortLogger(entry.logger) }}
            </span>
            <pre class="min-w-0 flex-1 whitespace-pre-wrap break-words text-gray-800">{{ entry.message }}</pre>
          </div>
        </div>
      </div>
    </div>

    <div
      v-else-if="activeTab === 'pushes'"
      class="bg-white border border-gray-200/80 rounded-2xl shadow-xs overflow-hidden"
    >
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-gray-100 px-5 py-3">
        <div class="flex items-center gap-2 text-sm font-semibold text-gray-900">
          <Cookie class="w-4 h-4 text-brand-600" />
          <span>Cookie 推送记录</span>
          <span class="rounded-full bg-gray-100 px-2 py-0.5 text-[10px] font-medium text-gray-500">
            {{ pushTotal }} 条
          </span>
          <span class="text-[10px] font-normal text-emerald-600">
            每 2 秒刷新
          </span>
        </div>

        <div class="flex flex-wrap items-center gap-2">
          <label class="inline-flex cursor-pointer select-none items-center gap-1.5 text-xs text-gray-600">
            <input
              v-model="onlyPushChanges"
              type="checkbox"
              class="h-3.5 w-3.5 rounded border-gray-300 text-brand-600 focus:ring-brand-500"
            >
            只看变化/异常
          </label>
          <Button
            size="sm"
            variant="secondary"
            :loading="pushFetching"
            @click="loadPushHistory()"
          >
            <RefreshCw class="w-3.5 h-3.5" />
            刷新
          </Button>
          <Button
            size="sm"
            variant="danger"
            :loading="clearingPushes"
            title="清空展示记录，不影响账号 Cookie"
            @click="clearPushHistory"
          >
            <Trash2 class="w-3.5 h-3.5" />
            清空
          </Button>
        </div>
      </div>

      <div class="border-b border-gray-100 bg-gray-50/60 px-5 py-2.5 text-[11px] leading-relaxed text-gray-500">
        只记录服务器接受的推送（含登录态失效）；不显示 Cookie 明文，只显示值的短指纹。
        与同一账号上一次推送对比；服务端保留最近 {{ pushCapacity }} 条，列表最多显示 100 条，重启后清空。
      </div>

      <div class="h-[62vh] overflow-y-auto bg-slate-50/40">
        <div
          v-if="filteredPushEntries.length === 0"
          class="flex h-full flex-col items-center justify-center gap-2 text-xs text-gray-400"
        >
          <Cookie class="h-8 w-8 text-gray-300" />
          <p v-if="pushEntries.length > 0">
            当前筛选下没有记录。取消「只看变化/异常」可查看全部。
          </p>
          <p v-else>
            暂无推送记录。油猴脚本成功推送一次后，这里会出现记录。
          </p>
        </div>

        <div
          v-for="entry in filteredPushEntries"
          :key="entry.seq"
          class="border-b border-gray-100 bg-white/70 px-5 py-3 last:border-0"
        >
          <div class="flex flex-wrap items-center gap-x-2 gap-y-1.5">
            <span class="text-[11px] text-gray-400">{{ formatPushTime(entry.at) }}</span>
            <span class="text-xs font-semibold text-gray-900">{{ entry.name }}</span>
            <span class="text-[11px] text-gray-400">
              {{ entry.email || '未识别邮箱' }} · u/{{ entry.auth_user }}
            </span>
            <span
              v-if="entry.source"
              class="text-[10px] text-gray-400"
            >
              {{ entry.source }}
            </span>
            <span
              class="rounded-full px-1.5 py-0.5 text-[10px] font-medium"
              :class="sessionBadgeClass(entry.session_valid)"
            >
              {{ sessionLabel(entry.session_valid) }}
            </span>
            <span
              class="rounded-full px-1.5 py-0.5 text-[10px] font-medium"
              :class="entry.injected ? 'bg-emerald-50 text-emerald-700' : 'bg-gray-100 text-gray-500'"
            >
              {{ entry.injected ? '已热注入' : '未注入' }}
            </span>
            <span
              v-if="entry.created"
              class="rounded-full bg-sky-50 px-1.5 py-0.5 text-[10px] font-medium text-sky-700"
            >
              新建账号
            </span>
            <span
              v-if="entry.updated_accounts > 1"
              class="rounded-full bg-sky-50 px-1.5 py-0.5 text-[10px] font-medium text-sky-700"
            >
              整组刷新 {{ entry.updated_accounts }}
            </span>
            <span
              class="rounded-full px-1.5 py-0.5 text-[10px] font-medium"
              :class="changeBadgeClass(entry)"
            >
              {{ changeLabel(entry) }}
            </span>
            <span
              v-if="entry.merged_changed && !entry.changed"
              class="rounded-full bg-violet-50 px-1.5 py-0.5 text-[10px] font-medium text-violet-700"
              title="推送罐没变，但服务端合并后的 auth.json 变了"
            >
              合并后变化
            </span>
            <button
              type="button"
              class="ml-auto inline-flex cursor-pointer items-center gap-0.5 rounded-lg px-1.5 py-1 text-[11px] text-gray-500 hover:bg-gray-100 hover:text-gray-800"
              @click="togglePush(entry.seq)"
            >
              <component
                :is="expandedPushes.has(entry.seq) ? ChevronUp : ChevronDown"
                class="h-3.5 w-3.5"
              />
              {{ expandedPushes.has(entry.seq) ? '收起' : '详情' }}
            </button>
          </div>

          <div class="mt-1 flex flex-wrap gap-x-4 gap-y-0.5 text-[11px] text-gray-500">
            <span>收到 {{ entry.received_count }} / 入库 {{ entry.cookie_count }}</span>
            <span>
              推送指纹
              <code class="font-mono text-gray-700">{{ entry.fingerprint }}</code>
            </span>
            <span>
              合并后指纹
              <code class="font-mono text-gray-700">{{ entry.merged_fingerprint }}</code>
            </span>
            <span
              v-if="entry.missing_advisory.length"
              class="text-amber-700"
            >
              推送罐缺 {{ entry.missing_advisory.join('、') }}
            </span>
          </div>

          <div
            v-if="expandedPushes.has(entry.seq)"
            class="mt-2 space-y-2 rounded-xl border border-gray-100 bg-gray-50/80 p-3"
          >
            <div
              v-if="!entry.first"
              class="text-[11px] text-gray-500"
            >
              指纹 <code class="font-mono">{{ entry.previous_fingerprint }}</code>
              → <code class="font-mono">{{ entry.fingerprint }}</code>
              <span class="ml-2">
                合并后 <code class="font-mono">{{ entry.merged_fingerprint }}</code>
              </span>
              <span
                v-if="entry.merged_changed"
                class="ml-1 text-violet-700"
              >
                （合并结果与上次不同）
              </span>
            </div>
            <div
              v-if="entry.first"
              class="text-[11px] text-gray-500"
            >
              这是该账号的第一条记录，没有可对比的上一次推送。完整罐共 {{ entry.cookie_count }} 个 Cookie。
            </div>
            <div
              v-else-if="entry.changes.length === 0"
              class="text-[11px] text-gray-500"
            >
              与上一次推送完全相同，没有 Cookie 名称或值发生变化。
            </div>
            <template v-else>
              <div
                v-for="group in changeGroups(entry)"
                :key="group.key"
              >
                <p
                  class="text-[11px] font-semibold"
                  :class="group.titleClass"
                >
                  {{ group.label }}（{{ group.items.length }}）
                </p>
                <ul class="mt-1 space-y-1">
                  <li
                    v-for="change in group.items"
                    :key="`${change.name}|${change.domain}|${change.path}`"
                    class="flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-[11px]"
                  >
                    <code class="font-mono font-medium text-gray-800">{{ change.name }}</code>
                    <span class="text-gray-400">{{ cookieLocation(change) }}</span>
                    <span
                      class="rounded px-1 py-0.5 text-[10px]"
                      :class="kindClass(change.kind)"
                    >
                      {{ kindLabel(change.kind) }}
                    </span>
                    <template v-if="change.kind === 'changed'">
                      <code class="font-mono text-gray-500">{{ change.before }}</code>
                      <ArrowRight class="h-3 w-3 text-gray-400" />
                      <code class="font-mono text-gray-800">{{ change.after }}</code>
                    </template>
                    <template v-else-if="change.kind === 'added'">
                      <span class="text-gray-500">新值指纹</span>
                      <code class="font-mono text-gray-800">{{ change.after }}</code>
                    </template>
                    <template v-else>
                      <span class="text-gray-500">旧值指纹</span>
                      <code class="font-mono text-gray-800">{{ change.before }}</code>
                    </template>
                  </li>
                </ul>
              </div>
            </template>
            <div
              v-if="entry.missing_advisory.length"
              class="text-[11px] text-amber-700"
            >
              推送罐缺少（Chrome 分区 Cookie 常见）：{{ entry.missing_advisory.join('、') }}
            </div>
            <div
              v-if="entry.merged_missing_advisory.length"
              class="text-[11px] text-gray-500"
            >
              合并后仍缺：{{ entry.merged_missing_advisory.join('、') }}
            </div>
          </div>
        </div>
      </div>
    </div>

    <div
      v-else
      class="bg-white border border-gray-200/80 rounded-2xl shadow-xs overflow-hidden"
    >
      <div class="flex items-center justify-between gap-2 border-b border-gray-100 px-5 py-3">
        <div class="flex items-center gap-2 text-sm font-semibold text-gray-900">
          <FileJson class="w-4 h-4 text-brand-600" />
          <span>请求转储</span>
          <span class="rounded-full bg-gray-100 px-2 py-0.5 text-[10px] font-medium text-gray-500">
            {{ dumpFiles.length }} 个文件
          </span>
        </div>
        <div class="flex items-center gap-2">
          <Button
            v-if="dumpFiles.length > 0"
            size="sm"
            variant="danger"
            @click="askClearDumps"
          >
            <Trash2 class="w-3.5 h-3.5" />
            清理全部
          </Button>
          <Button
            size="sm"
            variant="secondary"
            :loading="dumpLoading"
            @click="loadDumps"
          >
            <RefreshCw class="w-3.5 h-3.5" />
            刷新
          </Button>
        </div>
      </div>

      <div class="border-b border-gray-100 bg-gray-50/60 px-5 py-2.5 text-[11px] text-gray-500">
        存储目录：
        <code class="font-mono text-gray-700">{{ dumpDir || 'dumps' }}</code>
        <span class="ml-2">目录位于容器内；改成 /app/data/dumps 可随数据卷持久化。</span>
      </div>

      <div
        v-if="dumpFiles.length > 0"
        class="divide-y divide-gray-100"
      >
        <div
          v-for="file in dumpFiles"
          :key="file.name"
          class="flex items-center gap-2 px-5 py-3 transition-colors hover:bg-gray-50"
        >
          <button
            type="button"
            class="flex min-w-0 flex-1 items-center gap-3 text-left cursor-pointer"
            @click="openDump(file)"
          >
            <FileJson class="w-4 h-4 shrink-0 text-brand-500" />
            <div class="min-w-0 flex-1">
              <p class="truncate text-xs font-medium text-gray-800">
                {{ file.name }}
              </p>
              <p class="text-[11px] text-gray-400">
                {{ file.modified }} · {{ formatBytes(file.size) }}
              </p>
            </div>
            <span class="text-[11px] text-brand-600">查看</span>
          </button>
          <button
            type="button"
            class="shrink-0 rounded-lg p-1.5 text-gray-400 transition-colors hover:bg-rose-50 hover:text-rose-600 cursor-pointer"
            title="删除该转储文件"
            @click="askDeleteDump(file)"
          >
            <Trash2 class="w-4 h-4" />
          </button>
        </div>
      </div>

      <div
        v-else
        class="px-5 py-12 text-center text-xs text-gray-400"
      >
        <FileJson class="mx-auto mb-3 w-8 h-8 text-gray-300" />
        <p v-if="dumpLoading">
          加载中...
        </p>
        <p v-else>
          暂无转储文件。到「系统与模型配置 → 日志与调试」打开「报文持久化落盘」，再发一次请求即可生成。
        </p>
      </div>
    </div>

    <Modal
      v-model="dumpModalOpen"
      :title="selectedDumpName || '转储详情'"
      max-width="2xl"
    >
      <pre class="max-h-[60vh] overflow-auto whitespace-pre-wrap break-all rounded-xl bg-slate-900 p-4 text-[11px] leading-relaxed text-slate-100">{{ selectedDumpContent }}</pre>
    </Modal>

    <Modal
      v-model="deleteModalOpen"
      :title="deleteAll ? '清空全部转储' : '删除转储文件'"
      max-width="sm"
    >
      <p class="text-sm leading-relaxed text-gray-600">
        <template v-if="deleteAll">
          确定要删除全部 {{ dumpFiles.length }} 个转储文件吗？此操作不可恢复。
        </template>
        <template v-else>
          确定要删除 <span class="font-mono text-gray-900">{{ deleteTarget?.name }}</span> 吗？此操作不可恢复。
        </template>
      </p>
      <template #footer>
        <Button
          variant="secondary"
          @click="deleteModalOpen = false"
        >
          取消
        </Button>
        <Button
          variant="danger"
          :loading="deleting"
          @click="confirmDelete"
        >
          确认删除
        </Button>
      </template>
    </Modal>
  </div>
</template>
