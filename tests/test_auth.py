"""验收 B1–B6：自动建号、同手机重装找回、邮箱绑定、一个邮箱一个账号、新设备登录并合并。"""

from sqlalchemy import update

from app.models import EmailCode
from app.services import email as mailer


async def test_b1_first_open_creates_account_without_signup(phone):
    p = phone()
    body = await p.open_app()
    assert body["created"] is True and body["email"] is None
    me = (await p.get("/v1/account")).json()
    assert me["account_id"] == p.account_id and me["email"] is None


async def test_b3_reinstall_on_same_phone_returns_to_same_account(phone):
    p = phone()
    await p.open_app()
    await p.push(p.change("diary", "d1", {"text": "今天跑了 5 公里"}))
    first = p.account_id

    # 删 App 重装：钥匙串里的设备凭证还在，App 用它重新登录。
    p.token = ""
    body = await p.open_app()
    assert body["created"] is False and p.account_id == first
    assert (await p.pull_all())[("diary", "entry", "d1")]["payload"] == {"text": "今天跑了 5 公里"}


async def test_wrong_device_secret_is_rejected(phone, client):
    p = phone()
    await p.open_app()
    r = await client.post("/v1/auth/device", json={"device_id": p.device_id, "device_secret": "x" * 64})
    assert r.status_code == 401 and r.json()["detail"]["code"] == "device_secret_mismatch"


async def test_refresh_token(phone, client):
    p = phone()
    body = await p.open_app()
    r = await client.post("/v1/auth/refresh", json={"refresh_token": body["refresh_token"]})
    assert r.status_code == 200 and r.json()["account_id"] == p.account_id
    r = await client.post("/v1/auth/refresh", json={"refresh_token": body["access_token"]})
    assert r.status_code == 401


async def test_requests_without_token_are_rejected(client):
    assert (await client.get("/v1/sync/pull")).status_code == 401
    r = await client.get("/v1/sync/pull", headers={"Authorization": "Bearer nonsense"})
    assert r.status_code == 401


async def test_b4_bind_email_with_code(phone):
    p = phone()
    await p.open_app()
    r = await p.post("/v1/auth/email/code", json={"email": "Me@Example.com", "purpose": "bind"})
    assert r.status_code == 200
    to, code = mailer.outbox[-1]
    assert to == "me@example.com"

    wrong = "000000" if code != "000000" else "111111"
    r = await p.post("/v1/auth/email/bind", json={"email": "me@example.com", "code": wrong})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "code_invalid"

    r = await p.post("/v1/auth/email/bind", json={"email": "me@example.com", "code": code})
    assert r.status_code == 200
    assert (await p.get("/v1/account")).json()["email"] == "me@example.com"


async def test_b4_expired_code_fails(phone, db):
    p = phone()
    await p.open_app()
    await p.post("/v1/auth/email/code", json={"email": "a@example.com", "purpose": "bind"})
    async with db() as s:
        await s.execute(update(EmailCode).values(expires_at=0))
        await s.commit()
    r = await p.post("/v1/auth/email/bind", json={"email": "a@example.com", "code": mailer.outbox[-1][1]})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "code_expired"


async def test_code_locks_after_too_many_wrong_attempts(phone):
    p = phone()
    await p.open_app()
    await p.post("/v1/auth/email/code", json={"email": "a@example.com", "purpose": "bind"})
    code = mailer.outbox[-1][1]
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        await p.post("/v1/auth/email/bind", json={"email": "a@example.com", "code": wrong})
    r = await p.post("/v1/auth/email/bind", json={"email": "a@example.com", "code": code})
    assert r.json()["detail"]["code"] == "code_locked"


async def test_resend_too_soon_is_rejected(phone):
    p = phone()
    await p.open_app()
    assert (await p.post("/v1/auth/email/code", json={"email": "a@example.com", "purpose": "bind"})).status_code == 200
    r = await p.post("/v1/auth/email/code", json={"email": "a@example.com", "purpose": "bind"})
    assert r.status_code == 429 and r.json()["detail"]["code"] == "code_too_frequent"


async def test_b5_one_email_one_account(phone):
    a, b = phone(), phone()
    await a.open_app()
    await b.open_app()
    assert (await a.bind_email("same@example.com")).status_code == 200

    r = await b.post("/v1/auth/email/code", json={"email": "same@example.com", "purpose": "bind"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "email_taken" and r.json()["detail"]["can_login"] is True


async def test_login_with_unknown_email(phone):
    p = phone()
    await p.open_app()
    r = await p.post("/v1/auth/email/code", json={"email": "nobody@example.com", "purpose": "login"})
    assert r.status_code == 404


async def test_b6_new_phone_logs_in_and_merges(phone):
    old, new = phone(), phone()
    await old.open_app()
    await old.bind_email("me@example.com")
    await old.push(
        old.change("diary", "same-day", {"text": "旧手机写的"}, at=1_000),
        old.change("fitness", "plan", {"name": "旧计划"}, at=5_000),
    )

    await new.open_app()  # 新手机自动建了临时账号，并产生了数据
    temp_account = new.account_id
    temp_token = new.token
    await new.push(
        new.change("diary", "same-day", {"text": "新手机后来改的"}, at=2_000),  # 更新 → 新手机赢
        new.change("fitness", "plan", {"name": "新手机更早的计划"}, at=3_000),  # 更旧 → 原账号赢
        new.change("word", "progress", {"learned": 15}, at=4_000),  # 只有新手机有
    )

    r = await new.post("/v1/auth/email/code", json={"email": "me@example.com", "purpose": "login"})
    assert r.status_code == 200
    r = await new.post("/v1/auth/email/login", json={"email": "me@example.com", "code": mailer.outbox[-1][1]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["account_id"] == old.account_id and body["merged_records"] == 2
    new.token = body["access_token"]

    for p in (old, new):
        data = await p.pull_all()
        assert data[("diary", "entry", "same-day")]["payload"] == {"text": "新手机后来改的"}
        assert data[("fitness", "entry", "plan")]["payload"] == {"name": "旧计划"}
        assert data[("word", "entry", "progress")]["payload"] == {"learned": 15}

    # 临时账号已经并入并删除，旧令牌作废；新手机重装后回到原账号。
    assert (await new.client.get("/v1/account", headers={"Authorization": f"Bearer {temp_token}"})).status_code == 401
    new.token = ""
    await new.open_app()
    assert new.account_id == old.account_id != temp_account
