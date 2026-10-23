import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 浏览器客户端只访问本地服务；/api 代理到 FastAPI，避免开发期跨域配置。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
