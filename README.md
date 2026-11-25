# 资讯后台管理系统（v1.2.6）

轻量级自托管 Debian Docker 服务，专为 **本地采集生产 + Debian VPS 接收、一键编译与微信草稿箱管理** 架构打造，直接拉取预构建镜像运行，无需在服务器本地编译。

## 核心特性

- **零付费 AI API / 零模型 SDK**：容器内仅运行 FastAPI + SQLite（WAL 模式）+ `httpx`，绝不调用任何大模型 API。
- **资讯一键翻译推送（v1.2.5 新增）**：
  - 在「资讯管理」中支持一键将英文/外文资讯自动翻译为结构化中文文章（免费公共翻译接口 + 离线降级兜底），自动抓取原文核心段落；
  - 纯 Python 标准库生成标准 `1280x720` PNG 封面图，分配当日可用槽位 `YYYY-MM-DD-NN` 并立即推送至微信公众号草稿箱（`draft_only`）。
- **文章管理 & 发布记录联动清理（v1.2.5 新增）**：
  - 「文章管理」支持单条删除、勾选批量清理与一键清空，同步删除 SQLite 记录与 `/data/articles/`、`/data/incoming/` 下的全部本地文件及缓存；
  - 「发布记录」支持单条删除、勾选批量清理与一键清空，**删除发布记录时自动级联删除「文章管理」对应文章记录及其本地图片/缓存**，避免残留占用磁盘。
- **统一品牌与旧库自动迁移（v1.2.6 更新）**：
  - 系统标题与默认作者统一为 **「资讯后台管理系统」**，启动时自动将历史默认站点名迁移为新名称，不影响用户自定义名称。
- **开箱即用免本地编译**：默认直接拉取 `bbplin/ai-media-server:latest`（也可选通过 `.env` 的 `AI_MEDIA_IMAGE` 切换为 GHCR 镜像 `ghcr.io/yellowdking/ai-media-docker:latest`），`docker compose pull && docker compose up -d && docker image prune -f` 即可一键升级。
- **每日多篇分槽入库（`YYYY-MM-DD-01`..`99`）**：同时兼容单篇 `YYYY-MM-DD` 与每日多篇分槽目录 `YYYY-MM-DD-NN`，入库校验、预览、推草稿及自动清图均按槽位独立追踪。
- **严格草稿箱模式（`draft_only`）**：微信公众号接口仅上传永久封面素材与草稿箱（`draft/add`），**严禁自动群发**，支持 `access_token` 缓存/过期刷新与失败队列指数退避重试。
- **独立登录页 + HMAC-SHA256 Cookie 会话认证**：
  - 提供独立中文登录页与 `HttpOnly` + `SameSite=Lax` 的 `ai_media_session` Cookie（默认 24 小时，勾选「记住登录」7 天）；
  - 兼容 HTTP Basic Auth 供脚本调用；未认证 `/api/*` 统一返回 `401` JSON；
  - 仅绑定宿主机回环 `127.0.0.1:8088:8000`，公网经由 Nginx Proxy Manager 反代 `http://ai-media-server:8000`；`app_secret` 脱敏返回。
- **小硬盘友好（推完自动清图 + 7 天自动清理）**：
  - 真实推送微信草稿成功后**自动清理**该篇文章的本地图片文件及 `incoming/` 缓存并记录释放空间；
  - 后台每小时自动检查并清理超过保留天数（默认 **7 天**）的 `incoming/`、`articles/` 及历史 SQLite 记录，备份默认保留最新 **7 份**。

---

## 一键部署到新 Debian 服务器（`/opt/ai-media/`）

```bash
# 1. 克隆到 /opt/ai-media（严禁放 /root）
git clone https://github.com/yellowdking/ai-media-docker.git /opt/ai-media
cd /opt/ai-media

# 2. 准备目录与随机后台密码（chmod 600）
mkdir -p /opt/ai-media/data/{articles,incoming,backups,logs}
PW=$(python3 -c 'import secrets; print(secrets.token_urlsafe(16))')
SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(24))')
printf 'ADMIN_USER=admin\nADMIN_PASSWORD=%s\nSESSION_SECRET=%s\n' "$PW" "$SECRET" > .env
# 如需改用 GHCR 镜像，可追加：
# echo 'AI_MEDIA_IMAGE=ghcr.io/yellowdking/ai-media-docker:latest' >> .env
chmod 600 .env
cat .env

# 3. 如尚未创建 npm-network，先创建外部网络
docker network inspect npm-network >/dev/null 2>&1 || docker network create npm-network

# 4. 直接拉取镜像并启动（无需本地编译）
docker compose pull && docker compose up -d

# 5. 验证健康状态
curl -sf http://127.0.0.1:8088/health
```

### 手动 / CI 升级镜像

发布新 Release 并完成镜像构建后，在宿主机 `/opt/ai-media/` 执行以下命令拉取新镜像并重建容器：

```bash
cd /opt/ai-media && docker compose pull && docker compose up -d && docker image prune -f
```

### Nginx Proxy Manager 反代配置

- **Scheme**：`http`
- **Forward Hostname / IP**：`ai-media-server`
- **Forward Port**：`8000`
- 建议开启 SSL（HTTPS），并在微信公众平台将新服务器静态出口 IP 加入 **IP 白名单**。
