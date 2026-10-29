import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'

export default defineConfig({
  plugins: [vue(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 3000,
    proxy: {
      '/api': 'http://localhost:8080',
      '/auth': 'http://localhost:8080',
      '/stats': 'http://localhost:8080',
      '/accounts': 'http://localhost:8080',
      '/rotation': 'http://localhost:8080',
      '/config': 'http://localhost:8080',
      '/api-keys': 'http://localhost:8080',
      '/health': 'http://localhost:8080',
    },
  },
  build: {
    outDir: path.resolve(__dirname, '../src/aistudio_api/static'),
    emptyOutDir: true,
    rollupOptions: {
      output: {
        // 文件名带内容哈希：升级镜像后 index.html 引用新文件名，浏览器/代理
        // 不可能继续命中旧资源（固定文件名曾导致控制台升级后界面不更新）
        entryFileNames: 'assets/[name]-[hash].js',
        chunkFileNames: 'assets/[name]-[hash].js',
        assetFileNames: 'assets/[name]-[hash].[ext]',
        manualChunks: {
          'vendor-vue': ['vue', 'vue-router', 'pinia'],
          'vendor-icons': ['lucide-vue-next'],
          'vendor-yaml': ['yaml'],
        },
      },
    },
  },
})
