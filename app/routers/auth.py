import logging

from fastapi import APIRouter
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.deps import CurrentCaller, Session
from app.models import Account, Device, EmailCode, now_s
from app.schemas import (DeviceAuthIn, DeviceAuthOut, EmailCodeIn, EmailCodeOut, EmailLoginOut, EmailVerifyIn,
                         RefreshIn, TokenOut)
from app.security import ApiError, decode_token, hash_code, hash_secret, issue_tokens, new_code, same
from app.services import email as mailer
from app.services.sync import merge_into

router = APIRouter(prefix="/v1/auth", tags=["账号"])
log = logging.getLogger("bm.auth")


@router.post("/device", response_model=DeviceAuthOut)
async def device_auth(body: DeviceAuthIn, session: Session) -> dict:
    """App 第一次联网时调用：用钥匙串里的设备凭证建号或登录。不需要用户注册。"""
    device = await session.get(Device, body.device_id)
    created = False
    if device is None:
        account = Account()
        session.add(account)
        await session.flush()
        device = Device(id=body.device_id, account_id=account.id, secret_hash=hash_secret(body.device_secret))
        session.add(device)
        created = True
    else:
        if not same(device.secret_hash, hash_secret(body.device_secret)):
            raise ApiError(401, "device_secret_mismatch", "设备凭证不正确")
        account = await session.get(Account, device.account_id)
        device.last_seen_at = now_s()
    await session.commit()
    return {"account_id": account.id, "email": account.email, "created": created,
            **issue_tokens(account.id, device.id)}


@router.post("/refresh", response_model=TokenOut)
async def refresh(body: RefreshIn, session: Session) -> dict:
    claims = decode_token(body.refresh_token, "refresh")
    device = await session.get(Device, claims.get("did"))
    if device is None or device.account_id != claims.get("sub"):
        raise ApiError(401, "invalid_token", "令牌已失效，请重新登录")
    account = await session.get(Account, device.account_id)
    return {"account_id": account.id, "email": account.email, **issue_tokens(account.id, device.id)}


async def _owner_of(session: Session, email: str) -> Account | None:
    return (await session.execute(select(Account).where(Account.email == email))).scalar_one_or_none()


@router.post("/email/code", response_model=EmailCodeOut)
async def send_code(body: EmailCodeIn, caller: CurrentCaller, session: Session) -> dict:
    s = get_settings()
    email = body.email.lower()
    owner = await _owner_of(session, email)
    if body.purpose == "bind":
        if caller.account.email:
            raise ApiError(409, "account_has_email", "这个账号已经绑定了邮箱")
        if owner is not None:
            raise ApiError(409, "email_taken", "这个邮箱已绑定其他账号，可以改为用它登录", can_login=True)
    else:
        if owner is None:
            raise ApiError(404, "email_not_registered", "这个邮箱还没有绑定任何账号")
        if owner.id == caller.account.id:
            raise ApiError(409, "already_logged_in", "已经登录了这个账号")

    now = now_s()
    last = (await session.execute(
        select(EmailCode).where(EmailCode.email == email, EmailCode.purpose == body.purpose).order_by(EmailCode.created_at.desc(), EmailCode.id.desc())
    )).scalars().first()
    if last is not None and now - last.created_at < s.code_resend_seconds:
        wait = s.code_resend_seconds - (now - last.created_at)
        raise ApiError(429, "code_too_frequent", f"请 {wait} 秒后再发送", retry_after=wait)
    sent_last_hour = (await session.execute(
        select(func.count()).select_from(EmailCode)
        .where(EmailCode.account_id == caller.account.id, EmailCode.created_at > now - 3600)
    )).scalar_one()
    if sent_last_hour >= s.code_hourly_limit:
        raise ApiError(429, "code_hourly_limit", "发送次数太多，请一小时后再试", retry_after=3600)

    # 新验证码发出后，旧的同类验证码全部作废。
    await session.execute(update(EmailCode).where(
        EmailCode.email == email, EmailCode.purpose == body.purpose, EmailCode.consumed.is_(False)
    ).values(consumed=True))
    code = new_code()
    session.add(EmailCode(email=email, purpose=body.purpose, account_id=caller.account.id,
                          code_hash=hash_code(email, code), created_at=now,
                          expires_at=now + s.code_ttl_minutes * 60))
    await session.commit()
    await mailer.send_code(email, code, body.purpose)
    return {"expires_in": s.code_ttl_minutes * 60, "resend_after": s.code_resend_seconds}


async def _verify(session: Session, account_id: str, email: str, purpose: str, code: str) -> None:
    s = get_settings()
    row = (await session.execute(
        select(EmailCode).where(EmailCode.email == email, EmailCode.purpose == purpose,
                                EmailCode.account_id == account_id, EmailCode.consumed.is_(False))
        .order_by(EmailCode.created_at.desc(), EmailCode.id.desc())
    )).scalars().first()
    if row is None or row.expires_at < now_s():
        raise ApiError(400, "code_expired", "验证码已过期，请重新获取")
    if row.attempts >= s.code_max_attempts:
        raise ApiError(400, "code_locked", "错误次数太多，请重新获取验证码")
    if not same(row.code_hash, hash_code(email, code)):
        row.attempts += 1
        await session.commit()
        raise ApiError(400, "code_invalid", "验证码不正确", attempts_left=max(s.code_max_attempts - row.attempts, 0))
    row.consumed = True


@router.post("/email/bind")
async def bind_email(body: EmailVerifyIn, caller: CurrentCaller, session: Session) -> dict:
    email = body.email.lower()
    await _verify(session, caller.account.id, email, "bind", body.code)
    if caller.account.email:
        raise ApiError(409, "account_has_email", "这个账号已经绑定了邮箱")
    if await _owner_of(session, email) is not None:
        raise ApiError(409, "email_taken", "这个邮箱已绑定其他账号，可以改为用它登录", can_login=True)
    caller.account.email = email
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ApiError(409, "email_taken", "这个邮箱已绑定其他账号，可以改为用它登录", can_login=True)
    return {"account_id": caller.account.id, "email": email}


@router.post("/email/login", response_model=EmailLoginOut)
async def login_email(body: EmailVerifyIn, caller: CurrentCaller, session: Session) -> dict:
    """新设备登录原账号。这台设备原来是自动建的临时账号时，把它的数据并入原账号。"""
    email = body.email.lower()
    await _verify(session, caller.account.id, email, "login", body.code)
    target = await _owner_of(session, email)
    if target is None:
        raise ApiError(404, "email_not_registered", "这个邮箱还没有绑定任何账号")

    source = caller.account
    device = await session.get(Device, caller.device_id)
    device.account_id = target.id
    await session.flush()
    merged = 0
    if source.id != target.id and source.email is None:
        merged = await merge_into(session, source.id, target.id)
    await session.commit()
    log.info("device %s switched to account %s, merged %d records", device.id, target.id, merged)
    return {"account_id": target.id, "email": target.email, "merged_records": merged,
            **issue_tokens(target.id, device.id)}
