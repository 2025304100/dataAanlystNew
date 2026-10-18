import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
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
        // 用函数式分包：对象写法只列了 react/antd/echarts，其余 node_modules
        // （lucide-react、dayjs、rc-* 等）连同应用代码全部落进 index，导致
        // index 达 2.3MB 且 vendor-react 形同虚设。改为把所有 node_modules 归入
        // 各自 vendor 分包，index 只保留应用代码，便于按需/长期缓存。
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
