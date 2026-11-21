import json
import os
import tempfile
import zipfile

from fastapi import APIRouter, Response
from fastapi.responses import FileResponse
from sqlalchemy import select
from starlette.background import BackgroundTask

from app.deps import CurrentCaller, Session
from app.models import Media, Record
from app.schemas import ALLOWED_MEDIA, AccountOut
from app.services.storage import get_storage
from app.services.sync import delete_account_everything, module_switches

router = APIRouter(prefix="/v1/account", tags=["账号"])


@router.get("", response_model=AccountOut)
async def me(caller: CurrentCaller, session: Session) -> dict:
    a = caller.account
    return {"account_id": a.id, "email": a.email, "created_at": a.created_at,
            "modules": await module_switches(session, a.id)}


@router.delete("", status_code=204)
async def delete_account(caller: CurrentCaller, session: Session) -> Response:
    """删除账号：云端全部数据（记录、照片、语音、邮箱绑定、设备凭证）。手机本地数据由 App 保留。"""
    await delete_account_everything(session, caller.account.id)
    await session.commit()
    return Response(status_code=204)


@router.get("/export")
async def export(caller: CurrentCaller, session: Session) -> FileResponse:
    """一键导出：一个 zip，内含账号信息、模块开关、全部记录和照片语音原文件。"""
    a = caller.account
    records = (await session.execute(
        select(Record).where(Record.account_id == a.id, Record.deleted.is_(False)).order_by(Record.server_seq)
    )).scalars().all()
    media = (await session.execute(
        select(Media).where(Media.account_id == a.id, Media.status == "ready")
    )).scalars().all()
    switches = await module_switches(session, a.id)

    fd, path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    storage = get_storage()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("account.json", json.dumps(
            {"account_id": a.id, "email": a.email, "created_at": a.created_at}, ensure_ascii=False, indent=2))
        z.writestr("sync_settings.json", json.dumps(switches, ensure_ascii=False, indent=2))
        z.writestr("records.json", json.dumps([
            {"module": r.module, "kind": r.kind, "id": r.record_id, "modified_at_ms": r.modified_at_ms,
             "payload": r.payload} for r in records], ensure_ascii=False, indent=2))
        z.writestr("media.json", json.dumps([
            {"id": m.id, "module": m.module, "record_id": m.record_id, "content_type": m.content_type,
             "size": m.size, "sha256": m.sha256, "file": f"media/{m.id}.{ALLOWED_MEDIA[m.content_type]}"}
            for m in media], ensure_ascii=False, indent=2))
        for m in media:
            # 照片语音本身已压缩，不再重复压缩。
            z.writestr(f"media/{m.id}.{ALLOWED_MEDIA[m.content_type]}", await storage.read(m.object_key),
                       compress_type=zipfile.ZIP_STORED)
    return FileResponse(path, media_type="application/zip", filename="bm-export.zip",
                        headers={"X-Record-Count": str(len(records)), "X-Media-Count": str(len(media))},
                        background=BackgroundTask(os.remove, path))
