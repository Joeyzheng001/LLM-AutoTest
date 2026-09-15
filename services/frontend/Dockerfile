# 占位镜像：正式构建流程（vue-tsc + vite build → nginx）待 V2.1 裁决后启用
FROM nginx:1.27-alpine

COPY <<'EOF' /usr/share/nginx/html/index.html
<!doctype html>
<html lang="zh-CN">
  <head><meta charset="UTF-8" /><title>LLM-AutoTest</title></head>
  <body><p>frontend 占位（待 V2.1 范围裁决）</p></body>
</html>
EOF

EXPOSE 80
