import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 占位配置：dev 代理指向 orchestrator（契约未定稿，路径待 schemas/contracts/ 落地后调整）
export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
