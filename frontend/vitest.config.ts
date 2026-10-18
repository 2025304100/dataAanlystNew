import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    exclude: ["node_modules", "dist"],
    // 默认 5000ms 对含大量 AntD Select/Modal + userEvent 的交互用例过紧，
    // 高并发跑全量时会以 "Test timed out in 5000ms" 形式出现假红
    // （验收报告 §二/§四.2）。放宽到 15s，仍能兜住真正的死循环。
    testTimeout: 15000,
    // Windows + vitest 2.x 下依赖优化器会卡死，禁用 web 端优化器并内联核心依赖
    // 注意：deps.inline 虽已弃用但 vitest 2.1.9 下 enabled:false 时仍需配合 inline 使用
    deps: {
      optimizer: {
        web: {
          enabled: false,
        },
      },
      inline: ["antd", "@ant-design/icons", "react", "react-dom", "echarts", "echarts-for-react", "dayjs"],
    },
  },
});
