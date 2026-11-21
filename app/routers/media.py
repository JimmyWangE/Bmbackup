import time
import uuid

from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.config import get_settings
from app.deps import CurrentCaller, Session
from app.models import Media
from app.schemas import MediaOut, MediaUploadIn, UrlOut
from app.security import ApiError, same, sign_blob
from app.services.storage import LocalStorage, get_storage
from app.services.sync import module_switches

router = APIRouter(prefix="/v1/media", tags=["照片和语音"])


def _out(m: Media) -> dict:
    return {"id": m.id, "module": m.module, "record_id": m.record_id, "content_type": m.content_type,
            "size": m.size, "sha256": m.sha256, "status": m.status}


async def _own(session: Session, caller: CurrentCaller, media_id: str) -> Media:
    m = await session.get(Media, media_id.lower())
    if m is None or m.account_id != caller.account.id:
        raise ApiError(404, "media_not_found", "文件不存在")
    return m


@router.get("", response_model=list[MediaOut])
async def list_media(caller: CurrentCaller, session: Session) -> list[dict]:
    rows = (await session.execute(select(Media).where(Media.account_id == caller.account.id))).scalars().all()
    return [_out(m) for m in rows]


@router.post("/upload-url", response_model=UrlOut)
async def upload_url(body: MediaUploadIn, caller: CurrentCaller, session: Session) -> dict:
    s = get_settings()
    if not (await module_switches(session, caller.account.id))[body.module]:
        raise ApiError(403, "module_sync_disabled", "这个模块的同步已关闭")
    if body.size > s.max_media_bytes:
        raise ApiError(413, "media_too_large", f"文件不能超过 {s.max_media_bytes // 1024 // 1024} MB")
    media_id = body.id.lower()
    m = await session.get(Media, media_id)
    if m is not None and m.account_id != caller.account.id:
        raise ApiError(409, "media_id_conflict", "文件 ID 冲突，请换一个")
    if m is None:
        m = Media(id=media_id, account_id=caller.account.id, object_key=f"media/{uuid.uuid4()}")
        session.add(m)
    m.module, m.record_id, m.content_type = body.module, body.record_id, body.content_type
    m.size, m.sha256, m.status = body.size, body.sha256, "pending"
    await session.commit()
    return {"url": get_storage().upload_url(m.object_key, m.content_type), "method": "PUT",
            "headers": {"Content-Type": m.content_type}, "expires_in": s.media_url_seconds}


@router.post("/{media_id}/complete", response_model=MediaOut)
async def complete(media_id: str, caller: CurrentCaller, session: Session) -> dict:
    m = await _own(session, caller, media_id)
    size = await get_storage().size_of(m.object_key)
    if size is None:
        raise ApiError(400, "upload_missing", "还没有收到文件")
    if size != m.size:
        raise ApiError(400, "size_mismatch", f"文件大小不一致：声明 {m.size}，实际 {size}")
    m.status = "ready"
    await session.commit()
    return _out(m)


@router.get("/{media_id}/download-url", response_model=UrlOut)
async def download_url(media_id: str, caller: CurrentCaller, session: Session) -> dict:
    m = await _own(session, caller, media_id)
    if m.status != "ready":
        raise ApiError(409, "media_not_ready", "文件还没有上传完成")
    return {"url": get_storage().download_url(m.object_key), "method": "GET",
            "expires_in": get_settings().media_url_seconds}


@router.delete("/{media_id}", status_code=204)
async def delete_media(media_id: str, caller: CurrentCaller, session: Session) -> Response:
    m = await _own(session, caller, media_id)
    await get_storage().delete(m.object_key)
    await session.delete(m)
    await session.commit()
    return Response(status_code=204)


# ---------- 本地存储模式：预签名地址指向这里 ----------

def _local(key: str, method: str, exp: int, sig: str) -> LocalStorage:
    storage = get_storage()
    if not isinstance(storage, LocalStorage):
        raise ApiError(404, "not_found", "未启用本地存储")
    if exp < time.time() or not same(sig, sign_blob(key, method, exp)):
        raise ApiError(403, "bad_signature", "链接无效或已过期")
    return storage


@router.put("/blob/{key:path}", include_in_schema=False)
async def put_blob(key: str, exp: int, sig: str, request: Request, session: Session) -> Response:
    storage = _local(key, "PUT", exp, sig)
    m = (await session.execute(select(Media).where(Media.object_key == key))).scalar_one_or_none()
    if m is None:
        raise ApiError(404, "media_not_found", "文件不存在")
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > m.size:
            raise ApiError(413, "media_too_large", "文件比声明的大")
    await storage.write(key, bytes(data))
    return Response(status_code=200)


@router.get("/blob/{key:path}", include_in_schema=False)
async def get_blob(key: str, exp: int, sig: str, session: Session) -> FileResponse:
    storage = _local(key, "GET", exp, sig)
    m = (await session.execute(select(Media).where(Media.object_key == key))).scalar_one_or_none()
    path = storage.path(key)
    if m is None or not path.exists():
        raise ApiError(404, "media_not_found", "文件不存在")
    return FileResponse(path, media_type=m.content_type)
