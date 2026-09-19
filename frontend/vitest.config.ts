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
