# BM Backend

BM（Better Myself）iOS App 的后端。第一版提供：

- 自动建号（不用注册），绑定邮箱，用邮箱验证码在新设备登录原账号
- 按模块云同步：以最后修改为准，删除也同步，模块开关跟账号走
- 恋爱模块的照片和语音上传、下载
- 删除账号、一键导出云端数据

技术栈：Python 3.12 · FastAPI · PostgreSQL 16 · SQLAlchemy 2 · Alembic · Docker Compose + Caddy（自动 HTTPS）。

## 目录

```
app/
  main.py          入口、HTTPS 检查、/healthz
  config.py        全部配置（环境变量）
  models.py        数据表
  schemas.py       接口的请求和响应格式
  modules.py       可同步的模块列表（iOS 新增模块时在这里加）
  routers/         auth（账号）、sync（同步）、media（照片语音）、account（删除、导出）
  services/        sync（最后修改为准的合并）、storage（本地或 S3）、email（验证码）
migrations/        数据库迁移
tests/             对照验收标准的测试
scripts/           entrypoint.sh（容器启动）、smoke_test.py（部署后冒烟测试）
```

## 本地开发

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/pytest -q                      # 默认用 SQLite
TEST_DATABASE_URL=postgresql+asyncpg://bm@127.0.0.1:5432/bm_test .venv/bin/pytest -q   # 用 PostgreSQL
```

本地启动（需要一个 PostgreSQL）：

```bash
export DATABASE_URL=postgresql+asyncpg://bm@127.0.0.1:5432/bm EMAIL_BACKEND=console
.venv/bin/alembic upgrade head && .venv/bin/uvicorn app.main:app --reload
```

接口文档：启动后打开 `/docs`。

## 部署（单台服务器）

1. 准备一个域名，把它的 A 记录指向服务器 IP；服务器开放 80、443 端口。
2. 安装 Docker（含 compose 插件），然后执行：

   ```bash
   git clone https://github.com/JimmyWangE/Bmbackup.git && cd Bmbackup
   cp .env.example .env    # 填写 DOMAIN、POSTGRES_PASSWORD、SECRET_KEY、SMTP_*
   docker compose up -d --build
   python3 scripts/smoke_test.py https://<你的域名>
   ```

3. 更新版本：`git pull && docker compose up -d --build`（启动时自动执行数据库迁移）。
4. 备份：`docker compose exec db pg_dump -U bm bm > backup.sql`，以及 `media` 卷。

## iOS 怎么接

所有请求带 `Authorization: Bearer <access_token>`，错误统一是 `{"detail": {"code": "...", "message": "..."}}`。

| 步骤 | 接口 |
|---|---|
| 第一次启动：生成 `device_id`（UUID）和 `device_secret`（64 位随机十六进制），存进钥匙串 | `POST /v1/auth/device` |
| 令牌过期 | `POST /v1/auth/refresh` |
| 绑定邮箱：发码 → 填码 | `POST /v1/auth/email/code`（purpose=bind） → `POST /v1/auth/email/bind` |
| 新设备登录原账号（自动合并这台设备的临时账号） | `POST /v1/auth/email/code`（purpose=login） → `POST /v1/auth/email/login` |
| 模块开关 | `GET` / `PUT /v1/sync/settings` |
| 上传本地改动 | `POST /v1/sync/push` |
| 拉取改动（记住 `next_since`，`has_more` 为真就接着拉） | `GET /v1/sync/pull?since=` |
| 照片语音：要上传地址 → PUT 文件 → 确认 | `POST /v1/media/upload-url` → PUT → `POST /v1/media/{id}/complete` |
| 下载照片语音 | `GET /v1/media/{id}/download-url` |
| 删除账号（之后清掉钥匙串凭证，重新建号） | `DELETE /v1/account` |
| 导出 | `GET /v1/account/export`（zip） |

**一条同步记录**：

```json
{"module": "diary", "kind": "entry", "id": "2026-11-21", "modified_at_ms": 1763700000000,
 "deleted": false, "payload": { "...": "App 自己定义的内容，服务端不解析" }}
```

- `module` 必须是 `app/modules.py` 里的模块之一；`kind` 和 `id` 由 App 自己定。
- 同一个 `(module, kind, id)` 只保留 `modified_at_ms` 最大的那份；时间相同时按设备 ID 决定。
- 删除就发 `deleted: true`。
- 上传结果：`applied`（已写入）、`stale`（云端更新，拉取会拿到新版本）、`unchanged`（重复提交）、`skipped_disabled`（这个模块关了同步）。

**以后 iOS 加新功能**：用一个新的 `kind` 存数据即可，服务端不用改。加新模块：在 `app/modules.py` 里加名字。
