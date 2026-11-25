# AI Media Docker Server（微信公众号草稿管理后台）

轻量级自托管 Debian Docker 服务，专为 **Windows Antigravity 本地生产 + Debian VPS 接收与草稿箱管理** 架构打造，方便随时一键迁移到任意新机器。

## 核心特性

- **零付费 AI API / 零模型 SDK**：容器内仅运行 FastAPI + SQLite（WAL 模式）+ `httpx`，绝不调用任何大模型 API。
- **严格草稿箱模式（`draft_only`）**：微信公众号接口仅上传永久封面素材与草稿箱（`draft/add`），**严禁自动群发**，支持 `access_token` 缓存/过期刷新与失败队列指数退避重试。
- **公网安全隔离**：
  - 仅绑定宿主机回环 `127.0.0.1:8088:8000`（供 Windows 通过 SSH 同步与触发入库），公网端口完全隐藏；
  - 接入外部 `npm-network` 网络，通过 Nginx Proxy Manager 反代 `http://ai-media-server:8000` 访问；
  - 内置可配置 HTTP Basic Auth（`.env`），内部入库接口 `/api/internal/ingest/{date}` 严格限制仅本机回环调用，`app_secret` 脱敏返回。
- **小硬盘友好（7 天自动清理）**：后台每小时自动检查并清理超过 **7 天**的 `incoming/`、`articles/` 及历史 SQLite 记录，备份仅保留最新 **7 份**。
- **中文 10 模块管理后台**：资讯管理、选题管理、文章管理、移动端预览、图片素材、草稿上传、发布记录、运营数据、任务日志、系统配置。

---

## 一键部署到新 Debian 服务器（`/opt/ai-media/`）

```bash
# 1. 克隆到 /opt/ai-media（严禁放 /root）
git clone https://github.com/yellowdking/ai-media-docker.git /opt/ai-media
cd /opt/ai-media

# 2. 准备目录与随机后台密码（chmod 600）
mkdir -p /opt/ai-media/data/{articles,incoming,backups,logs}
PW=$(python3 -c 'import secrets; print(secrets.token_urlsafe(16))')
printf 'ADMIN_USER=admin\nADMIN_PASSWORD=%s\n' "$PW" > .env
chmod 600 .env
cat .env

# 3. 如尚未创建 npm-network，先创建外部网络
docker network inspect npm-network >/dev/null 2>&1 || docker network create npm-network

# 4. 构建并启动
docker compose up -d --build

# 5. 验证健康状态
curl -sf http://127.0.0.1:8088/health
```

### Nginx Proxy Manager 反代配置

- **Scheme**：`http`
- **Forward Hostname / IP**：`ai-media-server`
- **Forward Port**：`8000`
- 建议开启 SSL（HTTPS），并在微信公众平台将新服务器静态出口 IP 加入 **IP 白名单**。

### 换机器迁移步骤

1. 在新服务器按上述 5 步部署启动；
2. 登录管理后台「系统配置」重新填入微信公众号 `AppID` / `AppSecret` 并关闭模拟模式；
3. 将新服务器出口 IP 加入微信公众平台 IP 白名单；
4. 在 Windows 本地更新 `C:\AI-Media\config\sync.json` 中的 `host`、`port` 和 `key_path` 即可无缝衔接。
