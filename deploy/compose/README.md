# Docker Compose 正式部署指南

适合单台 Linux 服务器快速上线 YggdrasilBot-KOOK（自动包含 ParadeDB Postgres、Redis、迁移任务、bot 主进程、worker、scheduler，以及可选的 render / browser 服务）。

## 1. 准备环境变量

```bash
cp .env.compose.example .env
# 生成 YGG_SECRET_KEY（务必备份）
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

编辑 `.env`，至少填写：
- `POSTGRES_PASSWORD`：强密码
- `YGG_SECRET_KEY`：上面生成的 Fernet 密钥
- `YGG_SUPERUSERS`：你的 KOOK 用户 ID（JSON 字符串数组，如 `["12345678"]`）
- `YGG_ADMIN_EMAILS`：控制台管理员邮箱（如 `["admin@example.com"]`）
- `KOOK_BOT_TOKEN`（可选，也可启动后在控制台「设置 → KOOK 连接」里填写）

## 2. 启动服务

基础部署（Postgres + Redis + Migration + Bot + Worker + Scheduler）：

```bash
docker compose up -d --build
```

如需开启图片渲染（`render_*` 工具）或浏览器抓网页（`read_url` Chrome 兜底），先在 `.env` 取消对应 `YGG_RENDER_URL` / `YGG_BROWSER_URL` 注释，再带 profile 启动：

```bash
# 仅加 render
docker compose --profile render up -d --build
# 同时加 render + browser
docker compose --profile full up -d --build
```

## 3. 配置控制台反代鉴权

默认 `bot` 只监听本机 `127.0.0.1:8080`。生产模式（`YGG_ENV=prod`）通过反代传入的 `X-Auth-Request-Email` 鉴权：
- 轻量自用：参考 [`Caddyfile.example`](./Caddyfile.example) 用 Caddy + Basic Auth 注入固定管理员邮箱（注意清除客户端伪造的同名头）。
- 多人/团队：在 Nginx / Caddy / Traefik 前接 `oauth2-proxy`、Authelia 或 Cloudflare Access。

## 4. 升级与常用运维

```bash
# 查看状态与日志
docker compose ps
docker compose logs -f bot worker

# 拉取新代码并滚动更新（migration 会自动跑完再启动 bot/worker/scheduler）
git pull
docker compose up -d --build

# 备份 Postgres 数据库
docker compose exec postgres pg_dump -U ygg -Fc ygg > ygg_$(date +%F).dump
```
