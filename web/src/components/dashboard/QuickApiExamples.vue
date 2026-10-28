<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Code2, Terminal, Copy, Check, Cpu, Link2 } from 'lucide-vue-next'
import { useClipboard } from '@/composables/useClipboard.ts'

type Protocol = 'openai' | 'anthropic' | 'gemini'

interface ApiSnippet {
  title: string
  lang: string
  code: string
}

const props = withDefaults(
  defineProps<{
    models?: string[]
  }>(),
  {
    models: () => [],
  },
)

const DEFAULT_MODEL = 'gemini-3.7-flash'
const activeProtocol = ref<Protocol>('openai')
const selectedModel = ref('')
const { copied, copy } = useClipboard()

const origin = computed(() =>
  typeof window !== 'undefined' ? window.location.origin : 'http://localhost:8080',
)

const protocols: { id: Protocol; label: string; endpoint: string }[] = [
  { id: 'openai', label: 'OpenAI', endpoint: 'POST /v1/chat/completions' },
  { id: 'anthropic', label: 'Anthropic', endpoint: 'POST /v1/messages' },
  { id: 'gemini', label: 'Gemini', endpoint: 'POST /v1beta/models/{model}:generateContent' },
]

const modelOptions = computed(() => {
  const cleaned = props.models
    .map((name) => name.replace(/^models\//, ''))
    .filter((name) => name && !name.includes('embedding') && !name.includes('aqa'))
  return Array.from(new Set(cleaned)).sort((a, b) => a.localeCompare(b))
})

const defaultModel = computed(() => {
  const textModels = modelOptions.value.filter(
    (name) => !/(image|imagen|veo|tts|audio)/i.test(name),
  )
  const flash = textModels.find((name) => /flash/i.test(name))
  return flash || textModels[0] || modelOptions.value[0] || DEFAULT_MODEL
})

const model = computed(() => selectedModel.value || defaultModel.value)
const modelJson = computed(() => JSON.stringify(model.value))

watch(modelOptions, (options) => {
  if (selectedModel.value && !options.includes(selectedModel.value)) {
    selectedModel.value = ''
  }
})

const snippets = computed<ApiSnippet[]>(() => {
  const base = origin.value
  const name = model.value
  const modelLiteral = modelJson.value

  if (activeProtocol.value === 'openai') {
    return [
      {
        title: 'cURL',
        lang: 'bash',
        code: `curl ${base}/v1/chat/completions \\
  -H "Authorization: Bearer your-api-key" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": ${modelLiteral},
    "messages": [{"role": "user", "content": "你好"}],
    "stream": true
  }'`,
      },
      {
        title: 'Python (openai)',
        lang: 'python',
        code: `from openai import OpenAI

client = OpenAI(api_key="your-api-key", base_url="${base}/v1")

stream = client.chat.completions.create(
    model=${modelLiteral},
    messages=[{"role": "user", "content": "你好"}],
    stream=True,
)
for chunk in stream:
    delta = chunk.choices[0].delta.content
    if delta:
        print(delta, end="", flush=True)`,
      },
    ]
  }

  if (activeProtocol.value === 'anthropic') {
    return [
      {
        title: 'cURL',
        lang: 'bash',
        code: `curl ${base}/v1/messages \\
  -H "x-api-key: your-api-key" \\
  -H "anthropic-version: 2023-06-01" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": ${modelLiteral},
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "你好"}]
  }'`,
      },
      {
        title: 'Python (anthropic)',
        lang: 'python',
        code: `from anthropic import Anthropic

client = Anthropic(api_key="your-api-key", base_url="${base}")

message = client.messages.create(
    model=${modelLiteral},
    max_tokens=1024,
    messages=[{"role": "user", "content": "你好"}],
)
print(message.content[0].text)`,
      },
    ]
  }

  return [
    {
      title: 'cURL',
      lang: 'bash',
      code: `curl "${base}/v1beta/models/${name}:generateContent" \\
  -H "x-goog-api-key: your-api-key" \\
  -H "Content-Type: application/json" \\
  -d '{"contents": [{"role": "user", "parts": [{"text": "你好"}]}]}'`,
    },
    {
      title: 'Python (google-genai)',
      lang: 'python',
      code: `from google import genai

client = genai.Client(
    api_key="your-api-key",
    http_options={"api_version": "v1beta", "base_url": "${base}"},
)

response = client.models.generate_content(
    model="${name}",
    contents="你好",
)
print(response.text)`,
    },
  ]
})

const currentEndpoint = computed(
  () => protocols.find((item) => item.id === activeProtocol.value)?.endpoint || '',
)

function handleCopy(code: string, index: number) {
  void copy(code, `${activeProtocol.value}-${index}`, '已复制调用示例到剪贴板')
}

function handleCopyOrigin() {
  void copy(origin.value, 'origin', '已复制服务地址到剪贴板')
}
</script>

<template>
  <div class="bg-white border border-gray-200/80 rounded-2xl p-6 shadow-xs space-y-4">
    <div class="flex flex-col lg:flex-row lg:items-center justify-between gap-3 border-b border-gray-100 pb-3">
      <div class="flex items-center gap-2">
        <Code2 class="w-4 h-4 text-brand-600" />
        <h3 class="font-semibold text-gray-900 text-sm">
          快速调用示例
        </h3>
      </div>

      <div class="flex items-center gap-2 flex-wrap">
        <button
          type="button"
          class="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-[11px] font-mono text-gray-600 bg-gray-50 border border-gray-200 rounded-lg hover:bg-gray-100 transition-colors cursor-pointer"
          title="点击复制服务地址"
          @click="handleCopyOrigin"
        >
          <Link2 class="w-3.5 h-3.5 text-gray-400" />
          <span>{{ origin }}</span>
          <Check
            v-if="copied === 'origin'"
            class="w-3.5 h-3.5 text-emerald-500"
          />
          <Copy
            v-else
            class="w-3.5 h-3.5 text-gray-400"
          />
        </button>

        <div class="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-[11px] text-gray-600 bg-gray-50 border border-gray-200 rounded-lg">
          <Cpu class="w-3.5 h-3.5 text-gray-400" />
          <select
            v-model="selectedModel"
            class="bg-transparent outline-none cursor-pointer max-w-[180px]"
            title="选择示例使用的模型"
          >
            <option value="">
              自动 ({{ defaultModel }})
            </option>
            <option
              v-for="item in modelOptions"
              :key="item"
              :value="item"
            >
              {{ item }}
            </option>
          </select>
        </div>
      </div>
    </div>

    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
      <div class="flex items-center bg-gray-100 p-0.5 rounded-xl text-xs font-medium">
        <button
          v-for="item in protocols"
          :key="item.id"
          type="button"
          class="px-3 py-1.5 rounded-lg transition-all cursor-pointer"
          :class="
            activeProtocol === item.id
              ? 'bg-white text-gray-900 font-semibold shadow-2xs'
              : 'text-gray-500 hover:text-gray-900'
          "
          @click="activeProtocol = item.id"
        >
          {{ item.label }}
        </button>
      </div>

      <code class="text-[11px] font-mono text-gray-500 bg-gray-50 border border-gray-200/70 rounded-lg px-2.5 py-1 truncate">
        {{ currentEndpoint }}
      </code>
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
      <div
        v-for="(snippet, idx) in snippets"
        :key="`${activeProtocol}-${idx}`"
        class="bg-gray-900 text-gray-100 rounded-xl p-4 flex flex-col justify-between overflow-hidden shadow-xs"
      >
        <div class="flex items-center justify-between pb-2 border-b border-gray-800 text-xs font-medium text-gray-400">
          <div class="flex items-center gap-1.5">
            <Terminal class="w-3.5 h-3.5 text-brand-400" />
            <span>{{ snippet.title }}</span>
          </div>
          <button
            type="button"
            class="flex items-center gap-1 text-[11px] text-gray-400 hover:text-white px-2 py-0.5 rounded bg-gray-800 hover:bg-gray-700 transition-colors cursor-pointer"
            @click="handleCopy(snippet.code, idx)"
          >
            <Check
              v-if="copied === `${activeProtocol}-${idx}`"
              class="w-3 h-3 text-emerald-400"
            />
            <Copy
              v-else
              class="w-3 h-3"
            />
            <span>{{ copied === `${activeProtocol}-${idx}` ? '已复制' : '复制' }}</span>
          </button>
        </div>

        <pre class="mt-3 text-xs font-mono overflow-x-auto text-gray-200 leading-relaxed"><code>{{ snippet.code }}</code></pre>
      </div>
    </div>

    <p class="text-[11px] text-gray-400 leading-relaxed">
      兼容层内部仍使用 Gemini 模型 ID，OpenAI / Anthropic 客户端的 <code class="font-mono">model</code> 直接填 Gemini 模型名即可；工具调用、thinking 与图片输入会映射到对应字段。
    </p>
  </div>
</template>
