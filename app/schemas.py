from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.modules import MODULES

ID_PATTERN = r"^[A-Za-z0-9_.:-]+$"


def _check_module(v: str) -> str:
    if v not in MODULES:
        raise ValueError(f"未知模块 {v}，可用：{', '.join(MODULES)}")
    return v


# ---------- 账号 ----------

class DeviceAuthIn(BaseModel):
    device_id: str = Field(min_length=8, max_length=64, pattern=ID_PATTERN)
    device_secret: str = Field(min_length=32, max_length=256)


class RefreshIn(BaseModel):
    refresh_token: str


class TokenOut(BaseModel):
    account_id: str
    email: str | None
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class DeviceAuthOut(TokenOut):
    created: bool  # True 表示这次新建了账号


class EmailCodeIn(BaseModel):
    email: EmailStr
    purpose: Literal["bind", "login"]


class EmailCodeOut(BaseModel):
    sent: bool = True
    expires_in: int
    resend_after: int


class EmailVerifyIn(BaseModel):
    email: EmailStr
    code: str = Field(pattern=r"^\d{6}$")


class EmailLoginOut(TokenOut):
    merged_records: int  # 从这台设备原来的临时账号并入了多少条记录


class AccountOut(BaseModel):
    account_id: str
    email: str | None
    created_at: int
    modules: dict[str, bool]


# ---------- 同步 ----------

class SyncSettingsIn(BaseModel):
    modules: dict[str, bool]

    @field_validator("modules")
    @classmethod
    def known(cls, v: dict[str, bool]) -> dict[str, bool]:
        for m in v:
            _check_module(m)
        return v


class SyncSettingsOut(BaseModel):
    modules: dict[str, bool]


class ChangeIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    module: str
    kind: str = Field(min_length=1, max_length=64, pattern=ID_PATTERN)
    id: str = Field(min_length=1, max_length=128, pattern=ID_PATTERN)
    modified_at_ms: int = Field(gt=0)
    deleted: bool = False
    payload: Any = None

    @field_validator("module")
    @classmethod
    def known_module(cls, v: str) -> str:
        return _check_module(v)


class PushIn(BaseModel):
    changes: list[ChangeIn]


class ChangeResult(BaseModel):
    module: str
    kind: str
    id: str
    # applied：已写入；stale：云端有更新的版本（拉取后会拿到）；unchanged：重复提交；
    # skipped_disabled：这个模块的同步已关闭，没有上传。
    status: Literal["applied", "stale", "unchanged", "skipped_disabled"]
    server_seq: int | None


class PushOut(BaseModel):
    results: list[ChangeResult]


class ChangeOut(BaseModel):
    module: str
    kind: str
    id: str
    modified_at_ms: int
    deleted: bool
    payload: Any
    server_seq: int


class PullOut(BaseModel):
    changes: list[ChangeOut]
    next_since: int
    has_more: bool
    modules: dict[str, bool]


# ---------- 照片和语音 ----------

ALLOWED_MEDIA = {
    "image/jpeg": "jpg", "image/png": "png", "image/heic": "heic",
    "audio/m4a": "m4a", "audio/x-m4a": "m4a", "audio/mp4": "m4a", "audio/aac": "aac",
    "audio/mpeg": "mp3", "audio/wav": "wav",
}


class MediaUploadIn(BaseModel):
    id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    module: str = "love"
    record_id: str = Field(min_length=1, max_length=128, pattern=ID_PATTERN)
    content_type: str
    size: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("module")
    @classmethod
    def known_module(cls, v: str) -> str:
        return _check_module(v)

    @field_validator("content_type")
    @classmethod
    def allowed(cls, v: str) -> str:
        if v not in ALLOWED_MEDIA:
            raise ValueError(f"不支持的文件类型 {v}")
        return v


class UrlOut(BaseModel):
    url: str
    method: Literal["PUT", "GET"]
    headers: dict[str, str] = {}
    expires_in: int


class MediaOut(BaseModel):
    id: str
    module: str
    record_id: str
    content_type: str
    size: int
    sha256: str
    status: str
