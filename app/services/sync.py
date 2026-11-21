"""同步的核心规则：以最后修改为准（Last-Write-Wins）。"""

from dataclasses import dataclass

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Account, Device, EmailCode, Media, Record, SyncSetting
from app.modules import MODULES
from app.services.storage import get_storage


@dataclass
class Change:
    module: str
    kind: str
    record_id: str
    modified_at_ms: int
    deleted: bool
    payload: dict | list | None
    device_id: str


async def lock_account(session: AsyncSession, account_id: str) -> Account:
    """锁住账号行再分配序号，保证同一账号的并发写入序号不重复、不乱序。"""
    # populate_existing：会话里可能已经缓存了这个账号（鉴权时读过），必须用加锁后读到的最新序号覆盖。
    result = await session.execute(
        select(Account).where(Account.id == account_id).with_for_update().execution_options(populate_existing=True))
    return result.scalar_one()


async def module_switches(session: AsyncSession, account_id: str) -> dict[str, bool]:
    rows = (await session.execute(select(SyncSetting).where(SyncSetting.account_id == account_id))).scalars()
    switches = {m: True for m in MODULES}  # 默认全开
    for r in rows:
        if r.module in switches:
            switches[r.module] = r.enabled
    return switches


def incoming_wins(change: Change, current: Record) -> bool | None:
    """True：新的赢；False：旧的赢；None：完全相同的一次重复提交。"""
    new = (change.modified_at_ms, change.device_id)
    old = (current.modified_at_ms, current.device_id)
    if new == old:
        return None
    return new > old


async def apply_change(session: AsyncSession, account: Account, change: Change) -> tuple[str, int | None]:
    """写入一条改动，返回 (状态, 服务端序号)。调用方需要先锁住账号。"""
    current = (await session.execute(
        select(Record).where(
            Record.account_id == account.id, Record.module == change.module,
            Record.kind == change.kind, Record.record_id == change.record_id,
        )
    )).scalar_one_or_none()

    if current is not None:
        verdict = incoming_wins(change, current)
        if verdict is None:
            return "unchanged", current.server_seq
        if verdict is False:
            return "stale", current.server_seq

    account.sync_seq += 1
    seq = account.sync_seq
    if current is None:
        session.add(Record(
            account_id=account.id, module=change.module, kind=change.kind, record_id=change.record_id,
            payload=None if change.deleted else change.payload, deleted=change.deleted,
            modified_at_ms=change.modified_at_ms, device_id=change.device_id, server_seq=seq,
        ))
    else:
        current.payload = None if change.deleted else change.payload
        current.deleted = change.deleted
        current.modified_at_ms = change.modified_at_ms
        current.device_id = change.device_id
        current.server_seq = seq
    return "applied", seq


async def merge_into(session: AsyncSession, source_id: str, target_id: str) -> int:
    """把临时账号（新手机自动建的）的数据并入原账号，冲突以最后修改为准，然后删掉临时账号。"""
    target = await lock_account(session, target_id)
    records = (await session.execute(
        select(Record).where(Record.account_id == source_id).order_by(Record.server_seq)
    )).scalars().all()
    merged = 0
    for r in records:
        status, _ = await apply_change(session, target, Change(
            module=r.module, kind=r.kind, record_id=r.record_id, modified_at_ms=r.modified_at_ms,
            deleted=r.deleted, payload=r.payload, device_id=r.device_id))
        merged += status == "applied"
    # 照片和语音直接转给原账号；原账号已有同一个文件 ID 的，保留原账号的。
    target_media = set((await session.execute(select(Media.id).where(Media.account_id == target_id))).scalars())
    source_media = (await session.execute(select(Media).where(Media.account_id == source_id))).scalars().all()
    for m in source_media:
        if m.id in target_media:
            await get_storage().delete(m.object_key)
            await session.delete(m)
        else:
            m.account_id = target_id
    await session.flush()
    await delete_account_rows(session, source_id)
    return merged


async def delete_account_rows(session: AsyncSession, account_id: str) -> None:
    """删除一个账号在数据库里的全部数据（文件要在调用前先删）。"""
    for model in (Record, Media, SyncSetting, EmailCode, Device):
        await session.execute(delete(model).where(model.account_id == account_id))
    await session.execute(delete(Account).where(Account.id == account_id))


async def delete_account_everything(session: AsyncSession, account_id: str) -> None:
    storage = get_storage()
    keys = (await session.execute(select(Media.object_key).where(Media.account_id == account_id))).scalars().all()
    for key in keys:
        await storage.delete(key)
    await delete_account_rows(session, account_id)


async def touch_device(session: AsyncSession, device_id: str, now: int) -> None:
    await session.execute(update(Device).where(Device.id == device_id).values(last_seen_at=now))
