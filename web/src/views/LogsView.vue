<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref } from 'vue'
import { systemApi } from '@/api/system.ts'
import { useToastStore } from '@/stores/toast.ts'
import type { DumpFileInfo, LogEntry } from '@/types'
import Button from '@/components/ui/Button.vue'
import Modal from '@/components/ui/Modal.vue'
import {
  CirclePause,
  CirclePlay,
  FileJson,
  RefreshCw,
  Terminal,
  Trash2,
} from 'lucide-vue-next'

type TabKey = 'logs' | 'dumps'

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

function switchTab(tab: TabKey) {
  activeTab.value = tab
  if (tab === 'dumps') {
    void loadDumps()
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
    if (
      autoRefresh.value &&
      activeTab.value === 'logs' &&
      document.visibilityState !== 'hidden'
    ) {
      void loadLogs()
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
          在网页查看服务端最近日志与请求转储，不必登录服务器执行 docker logs。
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
