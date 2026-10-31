<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { systemApi } from '@/api/system.ts'
import { useToastStore } from '@/stores/toast.ts'
import type { SystemConfig, UpdateTimeoutsResponse } from '@/types/system.ts'
import Button from '@/components/ui/Button.vue'
import { Cpu, Pencil, X } from 'lucide-vue-next'

const props = defineProps<{
  initialConfig: SystemConfig | null
}>()

const emit = defineEmits<{
  saved: [res: UpdateTimeoutsResponse]
}>()

const toast = useToastStore()

const streamSeconds = ref(props.initialConfig?.timeout_stream || 600)
const replaySeconds = ref(props.initialConfig?.timeout_replay || 600)
const editing = ref(false)
const saving = ref(false)

watch(
  () => props.initialConfig,
  (cfg) => {
    if (!cfg || editing.value) return
    streamSeconds.value = cfg.timeout_stream || 600
    replaySeconds.value = cfg.timeout_replay || 600
  },
)

const draftStream = ref(streamSeconds.value)
const draftReplay = ref(replaySeconds.value)

function openEditor() {
  draftStream.value = streamSeconds.value
  draftReplay.value = replaySeconds.value
  editing.value = true
}

const draftValid = computed(
  () =>
    Number.isInteger(draftStream.value) &&
    Number.isInteger(draftReplay.value) &&
    draftStream.value >= 5 &&
    draftStream.value <= 86400 &&
    draftReplay.value >= 5 &&
    draftReplay.value <= 86400,
)

async function handleSave() {
  if (!draftValid.value || saving.value) return
  saving.value = true
  try {
    const res = await systemApi.updateTimeouts({
      stream: draftStream.value,
      replay: draftReplay.value,
    })
    streamSeconds.value = res.timeout_stream
    replaySeconds.value = res.timeout_replay
    editing.value = false
    if (res.persisted === false) {
      toast.info(
        `已生效：流式 ${res.timeout_stream}s / 非流式 ${res.timeout_replay}s，但配置文件不可写，重启后会恢复原值`,
      )
    } else {
      toast.success(
        `超时已更新：流式 ${res.timeout_stream}s / 非流式 ${res.timeout_replay}s`,
      )
    }
    emit('saved', res)
  } catch (err: unknown) {
    toast.error(err instanceof Error ? err.message : '更新超时失败')
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="bg-white border border-gray-200/80 rounded-2xl p-4 shadow-xs">
    <div class="flex items-center justify-between gap-2 mb-2">
      <div class="flex items-center gap-2 text-xs font-semibold text-gray-500 uppercase tracking-wider">
        <Cpu class="w-4 h-4 text-emerald-600" />
        <span>流式响应超时</span>
      </div>
      <button
        type="button"
        class="p-1 text-gray-300 hover:text-brand-600 hover:bg-brand-50 rounded-md transition-colors cursor-pointer"
        title="修改超时时间"
        @click="openEditor"
      >
        <Pencil class="w-3.5 h-3.5" />
      </button>
    </div>
    <div class="text-base font-bold text-gray-900 font-mono">
      {{ streamSeconds }}s
    </div>
    <div class="text-[11px] text-gray-400 mt-1">
      连续无数据才中断 · 非流式 {{ replaySeconds }}s
    </div>

    <div
      v-if="editing"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 backdrop-blur-xs"
      @click.self="editing = false"
    >
      <div class="bg-white rounded-2xl p-5 w-full max-w-md shadow-xl border border-gray-100 space-y-4">
        <div class="flex items-start justify-between gap-3">
          <div>
            <h3 class="font-bold text-gray-900 text-base">
              请求超时设置
            </h3>
            <p class="text-[11px] text-gray-500 mt-1">
              保存后立即生效，并写入 config.yaml 的 timeouts 段。
            </p>
          </div>
          <button
            type="button"
            class="p-1 text-gray-400 hover:text-gray-700 cursor-pointer"
            @click="editing = false"
          >
            <X class="w-4 h-4" />
          </button>
        </div>

        <div class="space-y-3 text-xs">
          <div>
            <label class="block font-medium text-gray-700 mb-1">
              流式响应超时（秒）
            </label>
            <input
              v-model.number="draftStream"
              type="number"
              min="5"
              max="86400"
              step="1"
              class="w-full px-3 py-2 border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-brand-500 font-mono"
            >
            <p class="text-[11px] text-gray-400 mt-1">
              每收到一段数据就会重新计时，这里是“两次数据之间最长等待”，不是整条回复的总时长。
            </p>
          </div>
          <div>
            <label class="block font-medium text-gray-700 mb-1">
              非流式请求超时（秒）
            </label>
            <input
              v-model.number="draftReplay"
              type="number"
              min="5"
              max="86400"
              step="1"
              class="w-full px-3 py-2 border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-brand-500 font-mono"
            >
            <p class="text-[11px] text-gray-400 mt-1">
              非流式 generateContent / 生图请求一次等待上游返回的最长时间。
            </p>
          </div>
          <p
            v-if="!draftValid"
            class="text-[11px] text-rose-600"
          >
            请输入 5 ~ 86400 之间的整数。
          </p>
        </div>

        <div class="flex justify-end gap-2 pt-2">
          <Button
            variant="outline"
            size="sm"
            @click="editing = false"
          >
            取消
          </Button>
          <Button
            variant="primary"
            size="sm"
            :loading="saving"
            :disabled="!draftValid"
            @click="handleSave"
          >
            保存
          </Button>
        </div>
      </div>
    </div>
  </div>
</template>
