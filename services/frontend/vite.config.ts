import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 占位配置：dev 代理指向 orchestrator（契约未定稿，路径待 schemas/contracts/ 落地后调整）
// 后端端口可用 ORCHESTRATOR_PORT 覆盖：本机 8000 常被其它项目（如 htftest-agent web）占用。
const backendPort = process.env.ORCHESTRATOR_PORT || '8000'

export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: {
      '/api': {
        target: `http://localhost:${backendPort}`,
        changeOrigin: true,
      },
    },
  },
})
