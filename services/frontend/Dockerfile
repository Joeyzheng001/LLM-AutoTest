# Vue 真实构建（V2.1 已批准完整 Web 界面；原占位镜像废弃）
# 构建上下文 = 仓库根（compose 已指向 ../）
# pnpm 为项目声明包管理器（packageManager 字段 + pnpm-lock.yaml，corepack 启用）
FROM node:20-alpine AS build

WORKDIR /build

RUN corepack enable

COPY services/frontend/package.json services/frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY services/frontend/ ./
RUN pnpm run build

FROM nginx:1.27-alpine

COPY --from=build /build/dist /usr/share/nginx/html
# 同源反代：/api → orchestrator（前端 client.ts 全部走相对路径）
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
