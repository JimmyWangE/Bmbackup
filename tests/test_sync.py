"""验收 C1–C6、C9（服务端部分）和 E2。"""

from app.modules import MODULES


async def two_phones(phone):
    """同一账号的 A、B 两台设备。"""
    from app.services import email as mailer

    a, b = phone(), phone()
    await a.open_app()
    await a.bind_email("me@example.com")
    await b.open_app()
    await b.post("/v1/auth/email/code", json={"email": "me@example.com", "purpose": "login"})
    r = await b.post("/v1/auth/email/login", json={"email": "me@example.com", "code": mailer.outbox[-1][1]})
    b.token, b.account_id = r.json()["access_token"], r.json()["account_id"]
    assert a.account_id == b.account_id
    return a, b


async def test_c1_all_modules_on_by_default(phone):
    p = phone()
    await p.open_app()
    modules = (await p.get("/v1/sync/settings")).json()["modules"]
    assert modules == {m: True for m in MODULES}


async def test_c2_change_on_a_reaches_b_for_every_module(phone):
    a, b = await two_phones(phone)
    await a.push(*[a.change(m, f"{m}-1", {"v": m}) for m in MODULES])
    data = await b.pull_all()
    for m in MODULES:
        assert data[(m, "entry", f"{m}-1")]["payload"] == {"v": m}


async def test_incremental_pull_and_paging(phone):
    p = phone()
    await p.open_app()
    await p.push(*[p.change("diary", f"d{i}", {"i": i}) for i in range(5)])
    r = (await p.get("/v1/sync/pull", params={"since": 0, "limit": 2})).json()
    assert len(r["changes"]) == 2 and r["has_more"] is True
    r = (await p.get("/v1/sync/pull", params={"since": r["next_since"], "limit": 10})).json()
    assert [c["payload"]["i"] for c in r["changes"]] == [2, 3, 4] and r["has_more"] is False
    r = (await p.get("/v1/sync/pull", params={"since": r["next_since"]})).json()
    assert r["changes"] == [] and r["has_more"] is False


async def test_c3_last_modified_wins_regardless_of_arrival_order(phone):
    a, b = await two_phones(phone)
    # 两台都离线：先 A 改（时间早），再 B 改（时间晚）。B 先联网上传，A 后上传。
    b_res = await b.push(b.change("diary", "2026-11-21", {"text": "B"}, at=2_000))
    a_res = await a.push(a.change("diary", "2026-11-21", {"text": "A"}, at=1_000))
    assert b_res[0]["status"] == "applied" and a_res[0]["status"] == "stale"
    for p in (a, b):
        assert (await p.pull_all())[("diary", "entry", "2026-11-21")]["payload"] == {"text": "B"}


async def test_same_timestamp_is_deterministic_and_resend_is_idempotent(phone):
    a, b = await two_phones(phone)
    await a.push(a.change("diary", "x", {"text": "A"}, at=5_000))
    await b.push(b.change("diary", "x", {"text": "B"}, at=5_000))
    winner = (await a.pull_all())[("diary", "entry", "x")]["payload"]["text"]
    expected = "A" if a.device_id > b.device_id else "B"
    assert winner == expected
    # 网络重试导致的重复提交不会产生新序号。
    res = await a.push(a.change("diary", "y", {"t": 1}, at=9_000))
    again = await a.push(a.change("diary", "y", {"t": 1}, at=9_000))
    assert again[0]["status"] == "unchanged" and again[0]["server_seq"] == res[0]["server_seq"]


async def test_c4_delete_syncs(phone):
    a, b = await two_phones(phone)
    await a.push(a.change("love", "m1", {"title": "第一次约会"}, kind="moment"))
    await a.push(a.change("love", "m1", None, kind="moment", deleted=True))
    c = (await b.pull_all())[("love", "moment", "m1")]
    assert c["deleted"] is True and c["payload"] is None


async def test_c4_love_trash_is_just_a_state_change(phone):
    a, b = await two_phones(phone)
    await a.push(a.change("love", "m1", {"title": "看海", "trashed_at": 1_700_000_000}, kind="moment"))
    c = (await b.pull_all())[("love", "moment", "m1")]
    assert c["deleted"] is False and c["payload"]["trashed_at"] == 1_700_000_000


async def test_c5_turning_off_a_module_follows_the_account(phone):
    a, b = await two_phones(phone)
    await a.push(a.change("diary", "d1", {"text": "关之前"}, at=1_000))

    r = await a.put("/v1/sync/settings", json={"modules": {"diary": False}})
    assert r.json()["modules"]["diary"] is False
    assert (await b.get("/v1/sync/settings")).json()["modules"]["diary"] is False  # B 的开关也关了

    res_a = await a.push(a.change("diary", "d1", {"text": "A 关闭后改"}, at=2_000),
                         a.change("sleep", "s1", {"h": 7}))
    res_b = await b.push(b.change("diary", "d2", {"text": "B 关闭后写"}, at=3_000))
    assert res_a[0]["status"] == "skipped_disabled" and res_a[1]["status"] == "applied"
    assert res_b[0]["status"] == "skipped_disabled"

    data = await b.pull_all()
    assert data[("diary", "entry", "d1")]["payload"] == {"text": "关之前"}  # 云端原有数据保留
    assert ("diary", "entry", "d2") not in data
    assert data[("sleep", "entry", "s1")]["payload"] == {"h": 7}  # 其他模块照常


async def test_c6_turning_back_on_uploads_and_merges(phone):
    a, b = await two_phones(phone)
    await a.push(a.change("diary", "d1", {"text": "v1"}, at=1_000))
    await a.put("/v1/sync/settings", json={"modules": {"diary": False}})
    await a.put("/v1/sync/settings", json={"modules": {"diary": True}})
    # 关闭期间本地改过的内容，重新打开后由 App 补传。
    await a.push(a.change("diary", "d1", {"text": "关闭期间改的"}, at=2_000))
    await b.push(b.change("diary", "d1", {"text": "B 更早"}, at=1_500))
    for p in (a, b):
        assert (await p.pull_all())[("diary", "entry", "d1")]["payload"] == {"text": "关闭期间改的"}


async def test_unknown_module_and_oversized_payload_are_rejected(phone):
    p = phone()
    await p.open_app()
    r = await p.post("/v1/sync/push", json={"changes": [p.change("nope", "x", {})]})
    assert r.status_code == 422
    r = await p.post("/v1/sync/push", json={"changes": [p.change("diary", "x", {"t": "字" * 300_000})]})
    assert r.status_code == 413
    r = await p.put("/v1/sync/settings", json={"modules": {"nope": False}})
    assert r.status_code == 422


async def test_e2_accounts_cannot_see_each_other(phone):
    x, y = phone(), phone()
    await x.open_app()
    await y.open_app()
    await x.push(x.change("diary", "secret", {"text": "X 的日记"}))
    assert await y.pull_all() == {}
    # Y 写同一个 ID 不会覆盖 X 的记录。
    await y.push(y.change("diary", "secret", {"text": "Y"}))
    assert (await x.pull_all())[("diary", "entry", "secret")]["payload"] == {"text": "X 的日记"}


async def test_concurrent_pushes_get_unique_sequence_numbers(phone):
    """同一账号多台设备同时上传，序号不重复、不丢（依赖 PostgreSQL 的行锁）。"""
    import asyncio
    import os

    import pytest

    if not os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql"):
        pytest.skip("并发写入只在 PostgreSQL 上测试")
    a, b = await two_phones(phone)
    batches = [(a if i % 2 else b).push(*[{"module": "diary", "kind": "entry", "id": f"c{i}-{j}",
                                          "modified_at_ms": 1_000 + j, "deleted": False, "payload": {"i": i}}
                                         for j in range(5)]) for i in range(10)]
    results = await asyncio.gather(*batches)
    seqs = [r["server_seq"] for batch in results for r in batch]
    assert len(seqs) == 50 and len(set(seqs)) == 50
    assert len(await a.pull_all()) == 50
