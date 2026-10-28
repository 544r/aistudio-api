<script setup lang="ts">
import { ref, watch } from 'vue'
import { useAuthStore } from '@/stores/auth.ts'
import { useToastStore } from '@/stores/toast.ts'
import { authApi } from '@/api/auth.ts'
import Modal from '@/components/ui/Modal.vue'
import Button from '@/components/ui/Button.vue'
import { KeyRound, Trash2 } from 'lucide-vue-next'

const props = defineProps<{
  modelValue: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [val: boolean]
}>()

const authStore = useAuthStore()
const toast = useToastStore()

const currentPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const errorMsg = ref('')
const saving = ref(false)

watch(
  () => props.modelValue,
  (val) => {
    if (val) {
      currentPassword.value = authStore.token
      newPassword.value = ''
      confirmPassword.value = ''
      errorMsg.value = ''
      saving.value = false
    }
  }
)

async function handleSave() {
  const current = currentPassword.value.trim()
  const next = newPassword.value.trim()
  errorMsg.value = ''

  if (!current) {
    errorMsg.value = '请输入当前密码'
    return
  }
  if (next.length < 8) {
    errorMsg.value = '新密码至少需要 8 个字符'
    return
  }
  if (next !== confirmPassword.value.trim()) {
    errorMsg.value = '两次输入的新密码不一致'
    return
  }
  if (next === current) {
    errorMsg.value = '新密码不能与当前密码相同'
    return
  }

  saving.value = true
  try {
    await authApi.changePassword(current, next)
    authStore.updateToken(next)
    toast.success('管理密码已更新，当前会话已切换')
    emit('update:modelValue', false)
  } catch (e) {
    const message = e instanceof Error ? e.message : '修改密码失败'
    errorMsg.value = message
    toast.error(message)
  } finally {
    saving.value = false
  }
}

function handleClearCache() {
  if (confirm('确定要清除本地保存的 Token 和缓存吗？')) {
    authStore.logout()
    currentPassword.value = ''
    newPassword.value = ''
    confirmPassword.value = ''
    toast.info('本地缓存已清除')
    emit('update:modelValue', false)
    window.location.reload()
  }
}
</script>

<template>
  <Modal
    :model-value="modelValue"
    title="修改管理密码"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="space-y-4">
      <div class="flex items-start gap-3 p-3 bg-brand-50/60 rounded-xl border border-brand-100 text-xs text-brand-900 leading-relaxed">
        <KeyRound class="w-4 h-4 text-brand-600 shrink-0 mt-0.5" />
        <div>
          修改后立即生效，无需重启容器；当前浏览器会自动切换到新密码，已登录的其他设备需要使用新密码重新登录。新密码以哈希形式保存在数据目录，重启后仍然有效。
        </div>
      </div>

      <div class="space-y-1.5">
        <label class="block text-xs font-semibold text-gray-700 uppercase tracking-wider">
          当前密码
        </label>
        <input
          v-model="currentPassword"
          type="password"
          autocomplete="current-password"
          placeholder="输入当前管理密码"
          class="w-full px-3.5 py-2.5 bg-gray-50 border border-gray-200 rounded-xl text-sm outline-none focus:bg-white focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 transition-all"
        >
      </div>

      <div class="space-y-1.5">
        <label class="block text-xs font-semibold text-gray-700 uppercase tracking-wider">
          新密码
        </label>
        <input
          v-model="newPassword"
          type="password"
          autocomplete="new-password"
          placeholder="至少 8 个字符"
          class="w-full px-3.5 py-2.5 bg-gray-50 border border-gray-200 rounded-xl text-sm outline-none focus:bg-white focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 transition-all"
        >
      </div>

      <div class="space-y-1.5">
        <label class="block text-xs font-semibold text-gray-700 uppercase tracking-wider">
          确认新密码
        </label>
        <input
          v-model="confirmPassword"
          type="password"
          autocomplete="new-password"
          placeholder="再次输入新密码"
          class="w-full px-3.5 py-2.5 bg-gray-50 border border-gray-200 rounded-xl text-sm outline-none focus:bg-white focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 transition-all"
          @keyup.enter="handleSave"
        >
      </div>

      <div
        v-if="errorMsg"
        class="px-3 py-2 rounded-xl bg-rose-50 border border-rose-200 text-xs text-rose-700"
      >
        {{ errorMsg }}
      </div>

      <div class="pt-2 border-t border-gray-100">
        <button
          type="button"
          class="w-full flex items-center justify-center gap-2 py-2 text-xs font-medium text-rose-600 hover:text-rose-700 hover:bg-rose-50 rounded-xl transition-colors cursor-pointer"
          @click="handleClearCache"
        >
          <Trash2 class="w-3.5 h-3.5" />
          <span>清除本地 Token 与缓存</span>
        </button>
      </div>
    </div>

    <template #footer>
      <Button
        variant="ghost"
        size="sm"
        :disabled="saving"
        @click="emit('update:modelValue', false)"
      >
        取消
      </Button>
      <Button
        variant="primary"
        size="sm"
        :loading="saving"
        @click="handleSave"
      >
        保存并生效
      </Button>
    </template>
  </Modal>
</template>
