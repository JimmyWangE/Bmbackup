#!/bin/sh
# 先把数据库升级到最新结构，再启动服务。
set -e
alembic upgrade head
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers "${WEB_WORKERS:-2}" --proxy-headers --forwarded-allow-ips="*"
