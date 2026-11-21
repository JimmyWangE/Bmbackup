"""数据库表。时间点一律存成毫秒/秒级时间戳整数，避免不同数据库对时区的处理差异。"""

import time
import uuid

from sqlalchemy import JSON, BigInteger, Boolean, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

JSONType = JSON().with_variant(JSONB(), "postgresql")


def now_s() -> int:
    return int(time.time())


def new_id() -> str:
    return str(uuid.uuid4())


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # 绑定的邮箱（小写），没绑定为空；一个邮箱只能属于一个账号。
    email: Mapped[str | None] = mapped_column(String(254), unique=True, nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, default=now_s)
    # 这个账号的同步序号，每写入一条记录加 1，用来增量拉取。
    sync_seq: Mapped[int] = mapped_column(BigInteger, default=0)


class Device(Base):
    """一台设备的凭证。device_id 和 secret 由 App 生成并存在钥匙串里，重装后还在。"""

    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    secret_hash: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[int] = mapped_column(BigInteger, default=now_s)
    last_seen_at: Mapped[int] = mapped_column(BigInteger, default=now_s)


class EmailCode(Base):
    __tablename__ = "email_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(254), index=True)
    purpose: Mapped[str] = mapped_column(String(16))  # bind / login
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    code_hash: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[int] = mapped_column(BigInteger, default=now_s)
    expires_at: Mapped[int] = mapped_column(BigInteger)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)


class SyncSetting(Base):
    """模块同步开关，跟账号走。没有这一行就当作打开（默认全开）。"""

    __tablename__ = "sync_settings"

    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True)
    module: Mapped[str] = mapped_column(String(32), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[int] = mapped_column(BigInteger, default=now_s)


class Record(Base):
    """通用同步记录。服务端不解析 payload，App 新增数据类型时服务端不用改。"""

    __tablename__ = "records"
    __table_args__ = (
        UniqueConstraint("account_id", "module", "kind", "record_id", name="uq_record_identity"),
        Index("ix_records_account_seq", "account_id", "server_seq"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    module: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(64))
    record_id: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict | list | None] = mapped_column(JSONType, nullable=True)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    # App 端最后修改时间（毫秒），「以最后修改为准」就比这个值。
    modified_at_ms: Mapped[int] = mapped_column(BigInteger)
    # 最后一次写入这条记录的设备，时间完全相同时用它决定谁赢，保证结果确定。
    device_id: Mapped[str] = mapped_column(String(64))
    server_seq: Mapped[int] = mapped_column(BigInteger)
    received_at: Mapped[int] = mapped_column(BigInteger, default=now_s)


class Media(Base):
    """照片和语音。文件本体在对象存储里，这里只记元数据。"""

    __tablename__ = "media"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    module: Mapped[str] = mapped_column(String(32))
    record_id: Mapped[str] = mapped_column(String(128))
    content_type: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    object_key: Mapped[str] = mapped_column(String(200), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending / ready
    created_at: Mapped[int] = mapped_column(BigInteger, default=now_s)
