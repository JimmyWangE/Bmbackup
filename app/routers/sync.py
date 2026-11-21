import json
import logging
import time

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.config import get_settings
from app.deps import CurrentCaller, Session
from app.models import Record, SyncSetting, now_s
from app.schemas import PullOut, PushIn, PushOut, SyncSettingsIn, SyncSettingsOut
from app.security import ApiError
from app.services.sync import Change, apply_change, lock_account, module_switches

router = APIRouter(prefix="/v1/sync", tags=["同步"])
log = logging.getLogger("bm.sync")


@router.get("/settings", response_model=SyncSettingsOut)
async def get_settings_(caller: CurrentCaller, session: Session) -> dict:
    return {"modules": await module_switches(session, caller.account.id)}


@router.put("/settings", response_model=SyncSettingsOut)
async def put_settings(body: SyncSettingsIn, caller: CurrentCaller, session: Session) -> dict:
    """开关跟账号走：一台设备改了，其他设备拉取时拿到同样的状态。关闭不删除云端数据。"""
    for module, enabled in body.modules.items():
        row = await session.get(SyncSetting, (caller.account.id, module))
        if row is None:
            session.add(SyncSetting(account_id=caller.account.id, module=module, enabled=enabled))
        else:
            row.enabled = enabled
            row.updated_at = now_s()
    await session.commit()
    return {"modules": await module_switches(session, caller.account.id)}


@router.post("/push", response_model=PushOut)
async def push(body: PushIn, caller: CurrentCaller, session: Session) -> dict:
    s = get_settings()
    if len(body.changes) > s.max_changes_per_push:
        raise ApiError(413, "too_many_changes", f"一次最多上传 {s.max_changes_per_push} 条")
    for c in body.changes:
        if len(json.dumps(c.payload, ensure_ascii=False).encode()) > s.max_payload_bytes:
            raise ApiError(413, "payload_too_large", f"记录 {c.kind}/{c.id} 太大")

    account = await lock_account(session, caller.account.id)
    switches = await module_switches(session, account.id)
    future_limit = int(time.time() * 1000) + 24 * 3600 * 1000
    results = []
    for c in body.changes:
        base = {"module": c.module, "kind": c.kind, "id": c.id}
        if not switches[c.module]:
            results.append({**base, "status": "skipped_disabled", "server_seq": None})
            continue
        if c.modified_at_ms > future_limit:
            # 手机时间明显超前时，它的修改会一直「最新」。先记日志，便于排查。
            log.warning("device %s sent modified_at_ms %d far in the future", caller.device_id, c.modified_at_ms)
        status, seq = await apply_change(session, account, Change(
            module=c.module, kind=c.kind, record_id=c.id, modified_at_ms=c.modified_at_ms,
            deleted=c.deleted, payload=c.payload, device_id=caller.device_id))
        results.append({**base, "status": status, "server_seq": seq})
    await session.commit()
    return {"results": results}


@router.get("/pull", response_model=PullOut)
async def pull(caller: CurrentCaller, session: Session,
               since: int = Query(0, ge=0), limit: int | None = Query(None, ge=1, le=2000)) -> dict:
    """返回序号大于 since 的全部改动（包括删除）。关闭同步的模块也照常下发云端已有数据。"""
    limit = limit or get_settings().pull_page_size
    rows = (await session.execute(
        select(Record).where(Record.account_id == caller.account.id, Record.server_seq > since)
        .order_by(Record.server_seq).limit(limit + 1)
    )).scalars().all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "changes": [{"module": r.module, "kind": r.kind, "id": r.record_id, "modified_at_ms": r.modified_at_ms,
                     "deleted": r.deleted, "payload": r.payload, "server_seq": r.server_seq} for r in rows],
        "next_since": rows[-1].server_seq if rows else since,
        "has_more": has_more,
        "modules": await module_switches(session, caller.account.id),
    }
