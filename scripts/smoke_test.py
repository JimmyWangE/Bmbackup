"""部署后的冒烟测试，只用 Python 标准库。

用法：python3 scripts/smoke_test.py https://api.example.com

会新建一个临时账号，走一遍建号、同步、照片上传下载、导出、删除账号，最后把这个临时账号删掉。
"""

import hashlib
import json
import secrets
import sys
import urllib.error
import urllib.request
import uuid

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8000"
token = ""


def call(method: str, path: str, body=None, *, raw: bytes | None = None, headers=None, url=None):
    h = {"Authorization": f"Bearer {token}"} if token else {}
    h.update(headers or {})
    data = raw
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url or BASE + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            content = r.read()
            return r.status, content, {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        return e.code, e.read(), {k.lower(): v for k, v in e.headers.items()}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("✅ " if ok else "❌ ") + name + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        sys.exit(1)


status, _, _ = call("GET", "/healthz")
check("健康检查 /healthz", status == 200, str(status))

if BASE.startswith("https://"):
    try:
        status, _, _ = call("GET", "/v1/account", url="http://" + BASE[len("https://"):] + "/v1/account")
    except Exception:  # noqa: BLE001 跳转到 https 后没带令牌也算拒绝
        status = 0
    check("E1 用 http 访问不会被当作正常请求处理", status != 200, str(status))

device = {"device_id": str(uuid.uuid4()), "device_secret": secrets.token_hex(32)}
status, body, _ = call("POST", "/v1/auth/device", device)
auth = json.loads(body)
check("B1 自动建号", status == 200 and auth.get("created") is True, body.decode()[:200])
token = auth["access_token"]

status, body, _ = call("POST", "/v1/auth/device", device)
check("B3 同一设备凭证回到同一账号", json.loads(body)["account_id"] == auth["account_id"])

status, body, _ = call("GET", "/v1/sync/settings")
check("C1 同步开关默认全开", status == 200 and all(json.loads(body)["modules"].values()))

change = {"module": "diary", "kind": "entry", "id": "smoke", "modified_at_ms": 2000, "payload": {"text": "smoke"}}
status, body, _ = call("POST", "/v1/sync/push", {"changes": [change]})
check("上传记录", status == 200 and json.loads(body)["results"][0]["status"] == "applied", body.decode()[:200])
status, body, _ = call("POST", "/v1/sync/push", {"changes": [{**change, "modified_at_ms": 1000, "payload": {}}]})
check("C3 更旧的修改不会覆盖", json.loads(body)["results"][0]["status"] == "stale")
status, body, _ = call("GET", "/v1/sync/pull?since=0")
check("拉取记录", status == 200 and json.loads(body)["changes"][0]["payload"] == {"text": "smoke"})

photo = b"\xff\xd8\xff" + secrets.token_bytes(2048)
media_id = str(uuid.uuid4())
status, body, _ = call("POST", "/v1/media/upload-url", {
    "id": media_id, "module": "love", "record_id": "smoke", "content_type": "image/jpeg",
    "size": len(photo), "sha256": hashlib.sha256(photo).hexdigest()})
check("获取照片上传地址", status == 200, body.decode()[:200])
up = json.loads(body)
status, body, _ = call("PUT", "", raw=photo, headers=up["headers"], url=up["url"])
check("上传照片", status in (200, 204), f"{status} {body[:200]!r}")
status, body, _ = call("POST", f"/v1/media/{media_id}/complete")
check("确认上传完成", status == 200, body.decode()[:200])
status, body, _ = call("GET", f"/v1/media/{media_id}/download-url")
saved = token
token = ""  # 预签名地址自带签名，不需要令牌
status, content, _ = call("GET", "", url=json.loads(body)["url"])
token = saved
check("C7 下载照片内容一致", status == 200 and content == photo)

status, content, headers = call("GET", "/v1/account/export")
check("B8 导出数据", status == 200 and content[:2] == b"PK" and headers.get("x-record-count") == "1")

status, _, _ = call("DELETE", "/v1/account")
check("B7 删除账号", status == 204)
status, _, _ = call("GET", "/v1/account")
check("B7 删除后旧令牌失效", status == 401)

print("\n全部通过 🎉")
