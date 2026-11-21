import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.db import dispose_engine, init_engine
from app.routers import account, auth, media, sync

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("bm")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    if s.env == "production":
        if s.secret_key.startswith("dev-only-change-me"):
            raise RuntimeError("生产环境必须设置 SECRET_KEY")
        if s.email_backend != "smtp":
            log.warning("EMAIL_BACKEND 不是 smtp，验证码不会真正发到邮箱")
    init_engine(s.database_url)
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(title="BM Backend", version="0.1.0", lifespan=lifespan,
                  description="BM iOS App 后端：自动建号、邮箱验证码绑定/登录、按模块云同步、照片语音、删除账号、导出数据。")

    @app.middleware("http")
    async def https_only(request: Request, call_next):
        # 生产环境只接受 HTTPS。TLS 由前面的 Caddy 终止，它会带上 X-Forwarded-Proto。
        if get_settings().require_https and request.url.path != "/healthz":
            if request.headers.get("x-forwarded-proto", request.url.scheme) != "https":
                return JSONResponse({"detail": {"code": "https_required", "message": "只接受 HTTPS 请求"}},
                                    status_code=403)
        return await call_next(request)

    @app.get("/healthz", tags=["运维"])
    async def healthz() -> dict:
        return {"ok": True}

    for r in (auth.router, sync.router, media.router, account.router):
        app.include_router(r)
    return app


app = create_app()
