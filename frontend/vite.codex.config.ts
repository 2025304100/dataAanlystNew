import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  cacheDir: 'C:/tmp/dataAanlystNew-vite-cache',
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/static': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: '../app/web/dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 1000,
    rollupOptions: {
      output: {
        // 与 vite.config.ts 保持一致：函数式分包，所有 node_modules 入 vendor 分包，
        // index 只留应用代码（详见 vite.config.ts 注释）。
        manualChunks(id) {
          if (!id.includes('node_modules')) return undefined
          if (id.includes('/echarts/') || id.includes('/echarts-for-react/') || id.includes('/zrender/')) return 'vendor-charts'
          if (
            id.includes('/antd/') ||
            id.includes('/@ant-design/') ||
            id.includes('/rc-') ||
            id.includes('/@rc-component/')
          ) return 'vendor-antd'
          if (id.includes('/react-dom/') || id.includes('/react/') || id.includes('/scheduler/') || id.includes('/react-is/')) return 'vendor-react'
          if (id.includes('/dayjs/')) return 'vendor-dayjs'
          if (id.includes('/lucide-react/')) return 'vendor-icons'
          return 'vendor-misc'
        },
      },
    },
  },
})
