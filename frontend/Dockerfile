# 多阶段构建：先 build，再用 nginx 托管静态产物并反向代理 /api。
# 这样浏览器同源访问前后端，云端不需要为后端开 CORS，安全性更好。

FROM node:20-alpine AS build
WORKDIR /app

# 有 lock 文件，用 npm ci 保证可复现
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
