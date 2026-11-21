"""测试默认用 SQLite；设置 TEST_DATABASE_URL 后改用 PostgreSQL（CI 里就是这样跑的）。"""

import os
import secrets
import uuid

import httpx
import pytest

os.environ.setdefault("ENV", "test")
os.environ["EMAIL_BACKEND"] = "memory"
os.environ["SECRET_KEY"] = "test-secret-key-0123456789-abcdefghijklmnop"
os.environ["PUBLIC_BASE_URL"] = "http://test"
os.environ["STORAGE_BACKEND"] = "local"

from app import db as dbmod  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import Base  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services import email as mailer  # noqa: E402
from app.services.storage import reset_storage  # noqa: E402


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/t.db"))
    get_settings.cache_clear()
    reset_storage()
    mailer.outbox.clear()
    engine = dbmod.init_engine(get_settings().database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await dbmod.dispose_engine()
    get_settings.cache_clear()
    reset_storage()


@pytest.fixture
def db():
    """直接操作数据库（例如把验证码改成已过期）。"""
    return dbmod._sessionmaker


class Phone:
    """模拟一台装了 BM 的手机：钥匙串里的设备凭证 + 当前令牌。"""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.device_id = str(uuid.uuid4())
        self.device_secret = secrets.token_hex(32)
        self.token = ""
        self.account_id = ""
        self.clock = 1_700_000_000_000

    async def open_app(self) -> dict:
        r = await self.client.post("/v1/auth/device",
                                   json={"device_id": self.device_id, "device_secret": self.device_secret})
        assert r.status_code == 200, r.text
        body = r.json()
        self.token, self.account_id = body["access_token"], body["account_id"]
        return body

    @property
    def h(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    async def get(self, url: str, **kw) -> httpx.Response:
        return await self.client.get(url, headers=self.h, **kw)

    async def post(self, url: str, **kw) -> httpx.Response:
        return await self.client.post(url, headers=self.h, **kw)

    async def put(self, url: str, **kw) -> httpx.Response:
        return await self.client.put(url, headers=self.h, **kw)

    async def delete(self, url: str, **kw) -> httpx.Response:
        return await self.client.delete(url, headers=self.h, **kw)

    async def push(self, *changes: dict) -> list[dict]:
        r = await self.post("/v1/sync/push", json={"changes": list(changes)})
        assert r.status_code == 200, r.text
        return r.json()["results"]

    async def pull_all(self, since: int = 0) -> dict[tuple[str, str, str], dict]:
        out: dict[tuple[str, str, str], dict] = {}
        while True:
            r = await self.get("/v1/sync/pull", params={"since": since})
            assert r.status_code == 200, r.text
            body = r.json()
            for c in body["changes"]:
                out[(c["module"], c["kind"], c["id"])] = c
            since = body["next_since"]
            if not body["has_more"]:
                return out

    def change(self, module: str, rid: str, payload=None, *, at: int | None = None, deleted: bool = False,
               kind: str = "entry") -> dict:
        self.clock += 1000
        return {"module": module, "kind": kind, "id": rid, "modified_at_ms": at or self.clock,
                "deleted": deleted, "payload": payload}

    async def bind_email(self, email: str) -> httpx.Response:
        r = await self.post("/v1/auth/email/code", json={"email": email, "purpose": "bind"})
        assert r.status_code == 200, r.text
        return await self.post("/v1/auth/email/bind", json={"email": email, "code": mailer.outbox[-1][1]})


@pytest.fixture
def phone(client):
    return lambda: Phone(client)
