from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Account, Device
from app.security import ApiError, decode_token

Session = Annotated[AsyncSession, Depends(get_session)]


@dataclass
class Caller:
    account: Account
    device_id: str


async def current_caller(session: Session, authorization: Annotated[str | None, Header()] = None) -> Caller:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ApiError(401, "not_authenticated", "缺少访问令牌")
    claims = decode_token(authorization[7:].strip(), "access")
    device = await session.get(Device, claims.get("did"))
    # 设备被删除、或者已经切换到别的账号（登录了原账号），旧令牌一律作废。
    if device is None or device.account_id != claims.get("sub"):
        raise ApiError(401, "invalid_token", "令牌已失效，请重新登录")
    account = await session.get(Account, device.account_id)
    if account is None:
        raise ApiError(401, "invalid_token", "账号不存在")
    return Caller(account=account, device_id=device.id)


CurrentCaller = Annotated[Caller, Depends(current_caller)]
