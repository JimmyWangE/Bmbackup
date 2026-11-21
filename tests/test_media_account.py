"""验收 C7（照片语音）、B7（删除账号）、B8（导出）、E1（只走 HTTPS）、E2（文件隔离）。"""

import hashlib
import io
import json
import uuid
import zipfile

from app.services import email as mailer
from tests.test_sync import two_phones

PHOTO = b"\xff\xd8\xff\xe0" + b"fake-jpeg-bytes" * 1000
VOICE = b"fake-m4a-audio" * 500


async def upload(p, data: bytes, content_type: str, record_id: str = "moment-1") -> str:
    media_id = str(uuid.uuid4())
    r = await p.post("/v1/media/upload-url", json={
        "id": media_id, "module": "love", "record_id": record_id, "content_type": content_type,
        "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    assert r.status_code == 200, r.text
    put = await p.client.put(r.json()["url"], content=data, headers=r.json()["headers"])
    assert put.status_code == 200, put.text
    r = await p.post(f"/v1/media/{media_id}/complete")
    assert r.status_code == 200 and r.json()["status"] == "ready", r.text
    return media_id


async def test_c7_photo_and_voice_reach_other_device(phone):
    a, b = await two_phones(phone)
    photo = await upload(a, PHOTO, "image/jpeg")
    voice = await upload(a, VOICE, "audio/m4a")
    await a.push(a.change("love", "moment-1", {"title": "海边", "photos": [photo], "voice": voice}, kind="moment"))

    moment = (await b.pull_all())[("love", "moment", "moment-1")]["payload"]
    for media_id, data in ((moment["photos"][0], PHOTO), (moment["voice"], VOICE)):
        r = await b.get(f"/v1/media/{media_id}/download-url")
        assert r.status_code == 200
        got = await b.client.get(r.json()["url"])
        assert got.status_code == 200 and got.content == data


async def test_media_rules(phone, client):
    p = phone()
    await p.open_app()
    media_id = str(uuid.uuid4())
    body = {"id": media_id, "module": "love", "record_id": "m", "content_type": "image/jpeg",
            "size": len(PHOTO), "sha256": hashlib.sha256(PHOTO).hexdigest()}
    r = await p.post("/v1/media/upload-url", json=body)
    assert (await p.post(f"/v1/media/{media_id}/complete")).json()["detail"]["code"] == "upload_missing"
    bad = r.json()["url"].replace("sig=", "sig=0")
    assert (await client.put(bad, content=PHOTO)).status_code == 403
    assert (await p.get(f"/v1/media/{media_id}/download-url")).status_code == 409  # 还没传完

    await p.put("/v1/sync/settings", json={"modules": {"love": False}})
    r = await p.post("/v1/media/upload-url", json={**body, "id": str(uuid.uuid4())})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "module_sync_disabled"

    r = await p.post("/v1/media/upload-url", json={**body, "id": str(uuid.uuid4()), "content_type": "text/html"})
    assert r.status_code == 422


async def test_e2_media_is_private(phone):
    x, y = phone(), phone()
    await x.open_app()
    await y.open_app()
    media_id = await upload(x, PHOTO, "image/jpeg")
    assert (await y.get(f"/v1/media/{media_id}/download-url")).status_code == 404
    assert (await y.delete(f"/v1/media/{media_id}")).status_code == 404
    assert (await y.get("/v1/media")).json() == []


async def test_b7_delete_account(phone, tmp_path):
    p = phone()
    await p.open_app()
    await p.bind_email("me@example.com")
    media_id = await upload(p, PHOTO, "image/jpeg")
    await p.push(p.change("love", "moment-1", {"photos": [media_id]}, kind="moment"))
    old_account, old_token = p.account_id, p.token
    assert any((tmp_path / "media").rglob("*"))

    assert (await p.delete("/v1/account")).status_code == 204

    assert not [f for f in (tmp_path / "media").rglob("*") if f.is_file()]  # 照片文件已删
    assert (await p.get("/v1/account")).status_code == 401  # 旧令牌作废

    # App 生成新的设备凭证，自动换成新账号，同步开关全开，本地数据上传到新账号。
    fresh = phone()
    body = await fresh.open_app()
    assert body["created"] is True and fresh.account_id != old_account
    assert all((await fresh.get("/v1/sync/settings")).json()["modules"].values())
    assert await fresh.pull_all() == {}

    # 原邮箱已释放：可以重新绑定；用它登录得到的不是原账号。
    assert (await fresh.bind_email("me@example.com")).status_code == 200
    assert (await fresh.get("/v1/account")).json()["account_id"] != old_account
    assert (await p.client.get("/v1/account", headers={"Authorization": f"Bearer {old_token}"})).status_code == 401


async def test_b7_same_device_credentials_after_delete_get_new_account(phone):
    p = phone()
    await p.open_app()
    old = p.account_id
    await p.delete("/v1/account")
    body = await p.open_app()
    assert body["created"] is True and p.account_id != old


async def test_b8_export_contains_everything(phone):
    p = phone()
    await p.open_app()
    media_id = await upload(p, PHOTO, "image/jpeg")
    await p.push(p.change("diary", "d1", {"text": "日记"}),
                 p.change("love", "moment-1", {"photos": [media_id]}, kind="moment"),
                 p.change("diary", "gone", None, deleted=True))
    r = await p.get("/v1/account/export")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    records = json.loads(z.read("records.json"))
    assert len(records) == 2 == int(r.headers["x-record-count"])
    assert z.read(f"media/{media_id}.jpg") == PHOTO
    assert json.loads(z.read("account.json"))["account_id"] == p.account_id


async def test_e1_https_only(phone, client, monkeypatch):
    from app.config import get_settings

    p = phone()
    await p.open_app()
    monkeypatch.setenv("REQUIRE_HTTPS", "true")
    get_settings.cache_clear()
    r = await p.get("/v1/account")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "https_required"
    r = await client.get("/v1/account", headers={**p.h, "X-Forwarded-Proto": "https"})
    assert r.status_code == 200
    assert (await client.get("/healthz")).status_code == 200


async def test_email_codes_are_not_shared_between_accounts(phone):
    """A 申请的登录验证码，不能被 B 拿去用。"""
    owner, a, b = phone(), phone(), phone()
    await owner.open_app()
    await owner.bind_email("me@example.com")
    await a.open_app()
    await b.open_app()
    await a.post("/v1/auth/email/code", json={"email": "me@example.com", "purpose": "login"})
    code = mailer.outbox[-1][1]
    r = await b.post("/v1/auth/email/login", json={"email": "me@example.com", "code": code})
    assert r.status_code == 400
